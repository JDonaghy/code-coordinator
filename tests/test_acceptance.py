"""Tests for coord/acceptance.py — manifest loading + verdict assembly (#944).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.acceptance import (
    ACCEPTANCE_DIRNAME,
    ForPathResolutionError,
    MANIFEST_FRAGMENTS_DIRNAME,
    MOCK_EXT_TO_DRIVER_KIND,
    ManifestData,
    ManifestError,
    acceptance_capability_gap,
    acceptance_root_for_driver,
    apply_expected_red,
    build_verdict,
    bug_contract_path,
    classify_expected_red_clear_result,
    clear_expected_red_entries,
    clear_expected_red_via_pr,
    dump_manifest_error_hint,
    expected_red_failure_summary,
    failure_summary,
    find_ms_manifest_for_issue_via_api,
    gate_a_contract_candidates,
    gate_a_contract_path,
    issue_dirname,
    list_expected_red_via_api,
    load_expected_red,
    load_manifest,
    missing_expected_red_warning,
    ms_dir_for_issue,
    oracle_loop_contract_block,
    parse_manifest_text,
    resolve_for_path,
    search_roots_for_repo,
)
# Aliased on import: pytest treats any module-level `test_*` name as a
# collectible test function, and `test_ids_for_issue` takes required
# positional args — importing it under its real name breaks collection.
from coord.acceptance import test_ids_for_issue as ids_for_issue
from coord.config import AcceptanceConfig, AcceptanceDriverConfig, Config
from coord.models import Machine, Repo


class TestAcceptanceRootForDriver:
    """#2896: which directory `coord acceptance run`/`record` actually reads
    a resolved driver's manifests/contracts from — the fix for the bug the
    hardcoded `base / ACCEPTANCE_DIRNAME` had once the tui-tuidriver route's
    slices moved out of the shared repo-root tree."""

    def test_no_entrypoint_falls_back_to_shared_tree(self, tmp_path: Path) -> None:
        """A directory-discovered driver (cli-pytest, no `entrypoint:`) keeps
        reading the repo-root ACCEPTANCE_DIRNAME — this repo's own ms-37
        slices never moved."""
        assert acceptance_root_for_driver(tmp_path, "") == tmp_path / ACCEPTANCE_DIRNAME

    def test_nested_entrypoint_resolves_to_its_sibling_dir(self, tmp_path: Path) -> None:
        """The tui-tuidriver route's real shape: entrypoint `tui/tests/
        acceptance.rs` -> manifests under `tui/tests/acceptance/`."""
        got = acceptance_root_for_driver(tmp_path, "tui/tests/acceptance.rs")
        assert got == tmp_path / "tui" / "tests" / "acceptance"

    def test_flat_entrypoint_collapses_onto_shared_tree(self, tmp_path: Path) -> None:
        """A repo whose entrypoint already sits at the tree root (e.g. a
        future standalone coord-tui repo's `tests/acceptance.rs`) resolves
        to the exact same directory a directory-discovered driver would."""
        got = acceptance_root_for_driver(tmp_path, "tests/acceptance.rs")
        assert got == tmp_path / ACCEPTANCE_DIRNAME


class TestLoadManifest:
    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert load_manifest(tmp_path / "tests" / "acceptance") == {}

    def test_flat_tests_shape(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        ms = root / "ms01"
        ms.mkdir(parents=True)
        (ms / "manifest.yml").write_text(
            "tests:\n  ms01::shows_menu: 944\n  ms01::selects_item: 944\n"
        )
        manifest = load_manifest(root)
        assert manifest == {"ms01::shows_menu": 944, "ms01::selects_item": 944}

    def test_grouped_issues_shape(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        ms = root / "ms01"
        ms.mkdir(parents=True)
        (ms / "manifest.json").write_text(
            '{"issues": {"944": ["ms01::a", "ms01::b"], "945": ["ms01::c"]}}'
        )
        manifest = load_manifest(root)
        assert manifest == {"ms01::a": 944, "ms01::b": 944, "ms01::c": 945}

    def test_merges_across_multiple_slices(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms01").mkdir(parents=True)
        (root / "ms02").mkdir(parents=True)
        (root / "ms01" / "manifest.yml").write_text("tests:\n  a: 1\n")
        (root / "ms02" / "manifest.yml").write_text("tests:\n  b: 2\n")
        manifest = load_manifest(root)
        assert manifest == {"a": 1, "b": 2}

    def test_malformed_yaml_raises_manifest_error(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms01").mkdir(parents=True)
        (root / "ms01" / "manifest.yml").write_text("tests: [this, is, not, a, mapping\n")
        with pytest.raises(ManifestError):
            load_manifest(root)

    def test_non_mapping_manifest_raises(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms01").mkdir(parents=True)
        (root / "ms01" / "manifest.yml").write_text("- a\n- b\n")
        with pytest.raises(ManifestError, match="must be a mapping"):
            load_manifest(root)

    def test_empty_manifest_file_is_skipped(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms01").mkdir(parents=True)
        (root / "ms01" / "manifest.yml").write_text("")
        assert load_manifest(root) == {}

    def test_reads_per_issue_fragments(self, tmp_path: Path) -> None:
        """#2543: two issues' slices under the SAME milestone, each writing
        only its own manifest.d/<issue>.yml fragment — never one shared
        file — merge into one mapping exactly like two ms-dirs' legacy
        manifest.yml files already did."""
        root = tmp_path / "tests" / "acceptance"
        frag_dir = root / "ms01" / MANIFEST_FRAGMENTS_DIRNAME
        frag_dir.mkdir(parents=True)
        (frag_dir / "944.yml").write_text("issues:\n  944: [ms01::a, ms01::b]\n")
        (frag_dir / "945.yml").write_text("issues:\n  945: [ms01::c]\n")
        manifest = load_manifest(root)
        assert manifest == {"ms01::a": 944, "ms01::b": 944, "ms01::c": 945}

    def test_merges_legacy_file_and_fragments_in_same_ms_dir(self, tmp_path: Path) -> None:
        """Backward compat (#2543): an already-merged legacy manifest.yml
        (this repo's own tests/acceptance/ms-33/manifest.yml shape) keeps
        working unchanged alongside NEW per-issue fragments landing in the
        same ms-dir going forward."""
        root = tmp_path / "tests" / "acceptance"
        ms = root / "ms01"
        ms.mkdir(parents=True)
        (ms / "manifest.yml").write_text("tests:\n  ms01::legacy: 900\n")
        frag_dir = ms / MANIFEST_FRAGMENTS_DIRNAME
        frag_dir.mkdir()
        (frag_dir / "944.yml").write_text("issues:\n  944: [ms01::a]\n")
        manifest = load_manifest(root)
        assert manifest == {"ms01::legacy": 900, "ms01::a": 944}

    def test_fragment_dir_with_non_manifest_files_ignores_them(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        frag_dir = root / "ms01" / MANIFEST_FRAGMENTS_DIRNAME
        frag_dir.mkdir(parents=True)
        (frag_dir / "944.yml").write_text("issues:\n  944: [ms01::a]\n")
        (frag_dir / "README.md").write_text("not a manifest fragment\n")
        assert load_manifest(root) == {"ms01::a": 944}


class TestTestIdsForIssue:
    def test_filters_by_issue(self) -> None:
        manifest = {"a": 1, "b": 1, "c": 2}
        assert ids_for_issue(manifest, 1) == {"a", "b"}
        assert ids_for_issue(manifest, 2) == {"c"}
        assert ids_for_issue(manifest, 3) == set()


class TestBuildVerdict:
    def test_counts_and_green(self) -> None:
        tests = [
            {"id": "a", "status": "pass"},
            {"id": "b", "status": "fail"},
            {"id": "c", "status": "skip"},
        ]
        verdict = build_verdict(tests, scope="issue", issue_number=944)
        assert verdict["total"] == 3
        assert verdict["passed"] == 1
        assert verdict["failed"] == 1
        assert verdict["skipped"] == 1
        assert verdict["green"] is False
        assert verdict["issue"] == 944
        assert verdict["scope"] == "issue"

    def test_green_when_all_pass(self) -> None:
        verdict = build_verdict([{"id": "a", "status": "pass"}], scope="all")
        assert verdict["green"] is True
        assert "issue" not in verdict

    def test_empty_is_not_green(self) -> None:
        verdict = build_verdict([], scope="all")
        assert verdict["green"] is False
        assert verdict["total"] == 0


class TestFailureSummary:
    def test_no_failures_is_empty_string(self) -> None:
        verdict = build_verdict([{"id": "a", "status": "pass"}], scope="all")
        assert failure_summary(verdict) == ""

    def test_lists_failures_with_messages(self) -> None:
        verdict = build_verdict(
            [{"id": "a", "status": "fail", "message": "expected 3 got 4"}],
            scope="all",
        )
        assert failure_summary(verdict) == "a: expected 3 got 4"

    def test_truncates_with_limit(self) -> None:
        tests = [{"id": f"t{i}", "status": "fail", "message": "x"} for i in range(7)]
        verdict = build_verdict(tests, scope="all")
        summary = failure_summary(verdict, limit=3)
        assert summary.count("\n") == 3  # 3 lines + "... and N more"
        assert "and 4 more" in summary


def test_dump_manifest_error_hint_mentions_authoring_issue(tmp_path: Path) -> None:
    hint = dump_manifest_error_hint(tmp_path / "tests" / "acceptance")
    assert "not been authored" in hint


class TestMsDirForIssue:
    def test_missing_dir_returns_none(self, tmp_path: Path) -> None:
        assert ms_dir_for_issue(tmp_path / "tests" / "acceptance", 945) is None

    def test_finds_owning_dir(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms01").mkdir(parents=True)
        (root / "ms02").mkdir(parents=True)
        (root / "ms01" / "manifest.yml").write_text("tests:\n  ms01::a: 944\n")
        (root / "ms02" / "manifest.yml").write_text("tests:\n  ms02::b: 945\n")
        assert ms_dir_for_issue(root, 945) == "ms02"
        assert ms_dir_for_issue(root, 944) == "ms01"

    def test_issue_not_in_any_manifest_returns_none(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms01").mkdir(parents=True)
        (root / "ms01" / "manifest.yml").write_text("tests:\n  ms01::a: 944\n")
        assert ms_dir_for_issue(root, 999) is None

    def test_malformed_manifest_propagates(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms01").mkdir(parents=True)
        (root / "ms01" / "manifest.yml").write_text("tests: [not, a, mapping\n")
        with pytest.raises(ManifestError):
            ms_dir_for_issue(root, 945)

    def test_finds_owning_dir_via_fragment(self, tmp_path: Path) -> None:
        """#2543: the issue's own manifest.d/<issue>.yml fragment resolves
        to its OWNING ms-dir name, not the fragment directory's own name
        ("manifest.d")."""
        root = tmp_path / "tests" / "acceptance"
        frag_dir = root / "ms01" / MANIFEST_FRAGMENTS_DIRNAME
        frag_dir.mkdir(parents=True)
        (frag_dir / "944.yml").write_text("issues:\n  944: [ms01::a]\n")
        assert ms_dir_for_issue(root, 944) == "ms01"


class TestParseManifestText:
    """#1138: parse_manifest_text is the shared parser behind both
    _parse_manifest_file (local disk) and the dispatch-time GitHub-fetch
    reader in coord.milestone_dispatch — exercise its ManifestData.exempt
    output directly rather than only through _parse_manifest_file's
    tests-only view."""

    def test_no_exempt_key_is_empty_frozenset(self) -> None:
        data = parse_manifest_text("tests:\n  a: 944\n")
        assert data == ManifestData(tests={"a": 944}, exempt=frozenset())

    def test_exempt_list_parsed(self) -> None:
        data = parse_manifest_text("tests:\n  a: 944\nexempt: [1125, 1130]\n")
        assert data.exempt == frozenset({1125, 1130})
        assert data.tests == {"a": 944}

    def test_non_list_exempt_ignored(self) -> None:
        data = parse_manifest_text("exempt: 1125\n")
        assert data.exempt == frozenset()

    def test_empty_text_returns_empty_data(self) -> None:
        assert parse_manifest_text("") == ManifestData()

    def test_malformed_yaml_raises_manifest_error(self) -> None:
        with pytest.raises(ManifestError):
            parse_manifest_text("tests: [not, a, mapping\n")

    def test_non_mapping_raises(self) -> None:
        with pytest.raises(ManifestError, match="must be a mapping"):
            parse_manifest_text("- a\n- b\n")


class TestExpectedRedParsing:
    """#2164: the ``expected_red:`` registry parsed off ManifestData."""

    def test_no_key_is_empty_dict(self) -> None:
        data = parse_manifest_text("tests:\n  a: 944\n")
        assert data.expected_red == {}

    def test_parses_issue_scoped_lists(self) -> None:
        data = parse_manifest_text(
            "tests:\n  a: 554\nexpected_red:\n  554:\n    - a\n    - b\n"
        )
        assert data.expected_red == {554: frozenset({"a", "b"})}

    def test_non_dict_value_ignored(self) -> None:
        data = parse_manifest_text("expected_red:\n  554: not-a-list\n")
        assert data.expected_red == {}

    def test_non_dict_expected_red_ignored(self) -> None:
        data = parse_manifest_text("expected_red: [1, 2]\n")
        assert data.expected_red == {}

    def test_non_integer_issue_key_ignored(self) -> None:
        data = parse_manifest_text("expected_red:\n  not-a-number:\n    - a\n")
        assert data.expected_red == {}


class TestLoadExpectedRed:
    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert load_expected_red(tmp_path / "tests" / "acceptance") == {}

    def test_flattens_issue_to_test_ids(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        ms = root / "ms11"
        ms.mkdir(parents=True)
        (ms / "manifest.yml").write_text(
            "tests:\n  a: 554\n  b: 554\nexpected_red:\n  554:\n    - a\n    - b\n"
        )
        assert load_expected_red(root) == {"a": 554, "b": 554}

    def test_merges_across_slices(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms11").mkdir(parents=True)
        (root / "ms12").mkdir(parents=True)
        (root / "ms11" / "manifest.yml").write_text(
            "expected_red:\n  554:\n    - a\n"
        )
        (root / "ms12" / "manifest.yml").write_text(
            "expected_red:\n  600:\n    - c\n"
        )
        assert load_expected_red(root) == {"a": 554, "c": 600}

    def test_no_expected_red_block_is_empty(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms11").mkdir(parents=True)
        (root / "ms11" / "manifest.yml").write_text("tests:\n  a: 554\n")
        assert load_expected_red(root) == {}

    def test_driver_kind_excludes_a_different_driver_milestone(
        self, tmp_path: Path
    ) -> None:
        # #2339: a repo routing coord/** -> cli-pytest and tui/** ->
        # tui-tuidriver must not merge a tui-tuidriver milestone's
        # expected_red into a cli-pytest run's registry (or vice versa) —
        # the other driver can never produce those test-ids, so an unscoped
        # merge always reported them `missing_expected_red_ids`, a hard CI
        # failure on every run of every OTHER driver the moment two
        # milestones used different drivers.
        root = tmp_path / "tests" / "acceptance"
        ms_tui = root / "ms65"
        ms_tui.mkdir(parents=True)
        (ms_tui / "manifest.yml").write_text("expected_red:\n  2282:\n    - board_tabs_2282::a\n")
        (ms_tui / "mocks").mkdir()
        (ms_tui / "mocks" / "board.screen").write_text("mock")

        ms_cli = root / "ms37"
        ms_cli.mkdir(parents=True)
        (ms_cli / "manifest.yml").write_text("expected_red:\n  1118:\n    - test_x\n")
        (ms_cli / "mocks").mkdir()
        (ms_cli / "mocks" / "usage.out").write_text("mock")

        assert load_expected_red(root, driver_kind="cli-pytest") == {"test_x": 1118}
        assert load_expected_red(root, driver_kind="tui-tuidriver") == {
            "board_tabs_2282::a": 2282
        }
        # Unfiltered (default) behaviour is unchanged — everything merges.
        assert load_expected_red(root) == {"board_tabs_2282::a": 2282, "test_x": 1118}

    def test_driver_kind_includes_a_milestone_with_no_mocks_dir(
        self, tmp_path: Path
    ) -> None:
        # A directory whose driver can't be determined (no mocks/, mixed
        # kinds, ...) must never be silently dropped — "unknown" is not
        # "safe to skip" for a hard-failure check.
        root = tmp_path / "tests" / "acceptance"
        (root / "issue-9").mkdir(parents=True)
        (root / "issue-9" / "manifest.yml").write_text(
            "expected_red:\n  9:\n    - bare_bug_test\n"
        )
        assert load_expected_red(root, driver_kind="web-playwright") == {
            "bare_bug_test": 9
        }

    def test_merges_per_issue_fragments(self, tmp_path: Path) -> None:
        """#2543: expected_red recorded in a per-issue manifest.d/<issue>.yml
        fragment merges exactly like a legacy shared manifest.yml's block
        did."""
        root = tmp_path / "tests" / "acceptance"
        frag_dir = root / "ms11" / MANIFEST_FRAGMENTS_DIRNAME
        frag_dir.mkdir(parents=True)
        (frag_dir / "554.yml").write_text("expected_red:\n  554:\n    - a\n")
        (frag_dir / "600.yml").write_text("expected_red:\n  600:\n    - c\n")
        assert load_expected_red(root) == {"a": 554, "c": 600}

    def test_driver_kind_resolves_ms_dir_for_a_fragment_path(self, tmp_path: Path) -> None:
        """#2543: driver_kind filtering must resolve the OWNING ms-dir's
        mocks/ for a fragment path (its grandparent), not the manifest.d/
        directory itself (which never has a mocks/ sibling)."""
        root = tmp_path / "tests" / "acceptance"
        ms_tui = root / "ms65"
        (ms_tui / "mocks").mkdir(parents=True)
        (ms_tui / "mocks" / "board.screen").write_text("mock")
        frag_dir = ms_tui / MANIFEST_FRAGMENTS_DIRNAME
        frag_dir.mkdir()
        (frag_dir / "2282.yml").write_text(
            "expected_red:\n  2282:\n    - board_tabs_2282::a\n"
        )

        ms_cli = root / "ms37"
        (ms_cli / "mocks").mkdir(parents=True)
        (ms_cli / "mocks" / "usage.out").write_text("mock")
        (ms_cli / "manifest.yml").write_text("expected_red:\n  1118:\n    - test_x\n")

        assert load_expected_red(root, driver_kind="cli-pytest") == {"test_x": 1118}
        assert load_expected_red(root, driver_kind="tui-tuidriver") == {
            "board_tabs_2282::a": 2282
        }


class TestApplyExpectedRed:
    def test_no_expected_red_ids_is_a_no_op(self) -> None:
        verdict = build_verdict([{"id": "a", "status": "fail"}], scope="all")
        result = apply_expected_red(verdict, set())
        assert result["ci_green"] == result["green"] is False
        assert result["unexpected_green"] == []
        assert result["expected_red_still_red"] == []

    def test_expected_red_failure_does_not_block_ci_green(self) -> None:
        """The whole point (#2164 acceptance criterion 1): a sealed slice
        authored red merges without turning the default branch red."""
        verdict = build_verdict(
            [
                {"id": "wide_label_paints_every_glyph", "status": "fail"},
                {"id": "ascii_label_is_unchanged", "status": "pass"},
            ],
            scope="all",
        )
        assert verdict["green"] is False
        result = apply_expected_red(verdict, {"wide_label_paints_every_glyph"})
        assert result["ci_green"] is True
        assert result["expected_red_still_red"] == ["wide_label_paints_every_glyph"]
        assert result["unexpected_green"] == []

    def test_expected_red_that_passes_is_a_hard_failure(self) -> None:
        """Acceptance criterion 2: an expected-red test that PASSES fails
        the run, loudly and distinguishably from an ordinary failure."""
        verdict = build_verdict(
            [{"id": "wide_label_paints_every_glyph", "status": "pass"}], scope="all",
        )
        assert verdict["green"] is True  # raw verdict looks fine...
        result = apply_expected_red(verdict, {"wide_label_paints_every_glyph"})
        assert result["ci_green"] is False  # ...but the CI-facing one isn't.
        assert result["unexpected_green"] == ["wide_label_paints_every_glyph"]

    def test_real_failure_alongside_expected_red_still_blocks(self) -> None:
        verdict = build_verdict(
            [
                {"id": "expected_red_id", "status": "fail"},
                {"id": "unrelated_regression", "status": "fail"},
            ],
            scope="all",
        )
        result = apply_expected_red(verdict, {"expected_red_id"})
        assert result["ci_green"] is False

    def test_empty_test_list_is_not_ci_green_even_with_expected_red(self) -> None:
        verdict = build_verdict([], scope="all")
        result = apply_expected_red(verdict, {"a"})
        assert result["ci_green"] is False

    def test_expected_red_id_that_never_ran_is_a_hard_failure(self) -> None:
        """#2164 review (non-blocking finding): an expected_red-listed
        test-id that vanishes from the driver's output entirely (broken
        entry point, deleted test) is neither a pass nor a fail — must
        still fail ci_green rather than silently not counting toward
        anything, mirroring `_scoped_verdict`'s own `missing_ids`."""
        verdict = build_verdict(
            [{"id": "still_here", "status": "pass"}], scope="all",
        )
        result = apply_expected_red(verdict, {"still_here", "vanished_test"})
        assert result["missing_expected_red_ids"] == ["vanished_test"]
        assert result["ci_green"] is False

    def test_no_missing_ids_when_all_expected_red_ids_ran(self) -> None:
        verdict = build_verdict([{"id": "a", "status": "fail"}], scope="all")
        result = apply_expected_red(verdict, {"a"})
        assert result["missing_expected_red_ids"] == []


class TestExpectedRedFailureSummary:
    def test_empty_when_no_unexpected_green(self) -> None:
        verdict = apply_expected_red(
            build_verdict([{"id": "a", "status": "fail"}], scope="all"), {"a"},
        )
        assert expected_red_failure_summary(verdict) == ""

    def test_names_the_hard_failure_distinctly(self) -> None:
        verdict = apply_expected_red(
            build_verdict([{"id": "a", "status": "pass"}], scope="all"), {"a"},
        )
        summary = expected_red_failure_summary(verdict)
        assert "HARD FAILURE" in summary
        assert "a" in summary
        assert "NOT an ordinary test failure" in summary


class TestClearExpectedRedEntries:
    ISSUE_EXAMPLE_TEXT = (
        "tests:\n"
        "  ms11_554_wide_tab_labels::wide_label_paints_every_glyph_in_its_own_columns: 554\n"
        "\n"
        "expected_red:\n"
        "  554:\n"
        "    - ms11_554_wide_tab_labels::wide_label_paints_every_glyph_in_its_own_columns\n"
        "    - ms11_554_wide_tab_labels::measured_tab_budget_matches_the_painted_width\n"
        "    # ascii_label_is_unchanged is deliberately absent — it is the control and must be green now\n"
    )

    def test_no_op_when_id_not_present(self) -> None:
        assert clear_expected_red_entries("tests:\n  a: 1\n", 1, {"nope"}) is None

    def test_no_op_when_cleared_ids_empty(self) -> None:
        assert clear_expected_red_entries(self.ISSUE_EXAMPLE_TEXT, 554, set()) is None

    def test_partial_clear_keeps_the_other_id_and_the_comment(self) -> None:
        result = clear_expected_red_entries(
            self.ISSUE_EXAMPLE_TEXT,
            554,
            {"ms11_554_wide_tab_labels::wide_label_paints_every_glyph_in_its_own_columns"},
        )
        assert result is not None
        # The cleared id's *list item* line is gone from expected_red — it
        # legitimately still appears once, in the untouched `tests:` block.
        assert result.count("wide_label_paints_every_glyph_in_its_own_columns") == 1
        assert "    - ms11_554_wide_tab_labels::measured_tab_budget_matches_the_painted_width" in result
        assert "deliberately absent" in result  # comment preserved
        assert "  554:" in result  # issue header preserved (one id remains)
        # Everything outside the expected_red block is untouched byte-for-byte.
        assert result.startswith(
            "tests:\n"
            "  ms11_554_wide_tab_labels::wide_label_paints_every_glyph_in_its_own_columns: 554\n"
        )

    def test_full_clear_drops_the_issue_block(self) -> None:
        result = clear_expected_red_entries(
            self.ISSUE_EXAMPLE_TEXT,
            554,
            {
                "ms11_554_wide_tab_labels::wide_label_paints_every_glyph_in_its_own_columns",
                "ms11_554_wide_tab_labels::measured_tab_budget_matches_the_painted_width",
            },
        )
        assert result is not None
        assert "expected_red" not in result
        assert "554:" not in result
        # The id legitimately still appears once, in the untouched `tests:`
        # block — only its expected_red list-item line is gone.
        assert result.count("wide_label_paints_every_glyph_in_its_own_columns") == 1
        # Unrelated content (the `tests:` block) is untouched.
        assert "tests:\n  ms11_554_wide_tab_labels" in result

    def test_result_is_parseable_and_reflects_the_clear(self) -> None:
        """Round-trip through parse_manifest_text — the whole point is that
        the coordinator can commit this text back as a valid manifest."""
        result = clear_expected_red_entries(
            self.ISSUE_EXAMPLE_TEXT,
            554,
            {"ms11_554_wide_tab_labels::wide_label_paints_every_glyph_in_its_own_columns"},
        )
        assert result is not None
        data = parse_manifest_text(result)
        assert data.expected_red == {
            554: frozenset({"ms11_554_wide_tab_labels::measured_tab_budget_matches_the_painted_width"})
        }
        assert data.tests == {
            "ms11_554_wide_tab_labels::wide_label_paints_every_glyph_in_its_own_columns": 554
        }

    def test_leaves_other_issues_alone(self) -> None:
        text = "expected_red:\n  1:\n    - a\n  2:\n    - b\n"
        result = clear_expected_red_entries(text, 1, {"a"})
        assert result is not None
        assert "2:" in result
        assert "- b" in result
        data = parse_manifest_text(result)
        assert data.expected_red == {2: frozenset({"b"})}

    def test_untouched_when_manifest_has_no_expected_red_block(self) -> None:
        assert clear_expected_red_entries("tests:\n  a: 1\n", 1, {"a"}) is None


class TestClearExpectedRedEntriesQuotedIds:
    """#2296: every Playwright manifest id is double-quoted (an unquoted
    leading `[chromium]` would parse as a YAML flow sequence, not a string),
    so the clearer must compare the *parsed* scalar, not the raw line text,
    or it can never match a real web-playwright manifest."""

    PLAYWRIGHT_ID = "[chromium] ms-1/84-front-door.spec.ts › placeholder"

    def test_double_quoted_id_is_cleared(self) -> None:
        text = (
            "expected_red:\n"
            f'  84:\n    - "{self.PLAYWRIGHT_ID}"\n'
        )
        result = clear_expected_red_entries(text, 84, {self.PLAYWRIGHT_ID})
        assert result is not None
        assert "expected_red" not in result
        data = parse_manifest_text(text)
        assert data.expected_red == {84: frozenset({self.PLAYWRIGHT_ID})}

    def test_single_quoted_id_is_cleared(self) -> None:
        text = "expected_red:\n  1:\n    - 'plain single quoted'\n"
        result = clear_expected_red_entries(text, 1, {"plain single quoted"})
        assert result is not None
        assert "expected_red" not in result

    def test_single_quoted_id_with_embedded_quote_escape_is_cleared(self) -> None:
        # YAML's single-quote escape for an embedded `'` is `''`.
        text = "expected_red:\n  1:\n    - 'it''s red'\n"
        data = parse_manifest_text(text)
        assert data.expected_red == {1: frozenset({"it's red"})}
        result = clear_expected_red_entries(text, 1, {"it's red"})
        assert result is not None
        assert "expected_red" not in result

    def test_plain_unquoted_id_still_clears(self) -> None:
        """The unquoted case already worked before #2296 — must keep working."""
        text = "expected_red:\n  1:\n    - plain_unquoted_id\n"
        result = clear_expected_red_entries(text, 1, {"plain_unquoted_id"})
        assert result is not None
        assert "expected_red" not in result

    def test_id_containing_a_hash_is_cleared_not_truncated_as_a_comment(self) -> None:
        """Inside a quoted scalar, `#` is data, not a comment delimiter —
        `sub_line.split("#", 1)` would truncate the id and miss it."""
        id_with_hash = "issue #84 regression check"
        text = f'expected_red:\n  1:\n    - "{id_with_hash}"\n'
        data = parse_manifest_text(text)
        assert data.expected_red == {1: frozenset({id_with_hash})}
        result = clear_expected_red_entries(text, 1, {id_with_hash})
        assert result is not None
        assert "expected_red" not in result

    def test_quoted_id_with_trailing_comment_is_still_cleared(self) -> None:
        text = (
            "expected_red:\n"
            f'  84:\n    - "{self.PLAYWRIGHT_ID}"  # flaky, see #91\n'
        )
        result = clear_expected_red_entries(text, 84, {self.PLAYWRIGHT_ID})
        assert result is not None
        assert "expected_red" not in result

    def test_mixed_quoted_and_plain_partial_clear_preserves_the_other(self) -> None:
        text = (
            "expected_red:\n"
            "  84:\n"
            f'    - "{self.PLAYWRIGHT_ID}"\n'
            "    - plain_id_stays\n"
        )
        result = clear_expected_red_entries(text, 84, {self.PLAYWRIGHT_ID})
        assert result is not None
        assert "plain_id_stays" in result
        data = parse_manifest_text(result)
        assert data.expected_red == {84: frozenset({"plain_id_stays"})}

    def test_playwright_manifest_reproduction(self) -> None:
        """The exact reproduction from #2296: a manifest with several
        double-quoted Playwright ids has ALL of them cleared and returns
        updated (non-None) text."""
        ids = [f"[chromium] ms-1/84-front-door.spec.ts › case {i}" for i in range(6)]
        body = "\n".join(f'    - "{i}"' for i in ids)
        text = f"expected_red:\n  84:\n{body}\n"
        parsed_ids = parse_manifest_text(text).expected_red[84]
        assert len(parsed_ids) == 6
        result = clear_expected_red_entries(text, 84, parsed_ids)
        assert result is not None
        assert "expected_red" not in result


class _FakeApiGhOps:
    """Stub for the GitHub-API-only surface #2164's post-merge clearing
    sweep needs — no `gh` subprocess, no local checkout. Mirrors
    `coord.merge_queue.GhOps`'s "optional attribute" test-stub convention:
    a test that wants to exercise the "gh_ops doesn't support this" path
    can just not define one of these methods."""

    def __init__(self, files: dict[str, str], subdirs: list[str] | None = None):
        # path -> text. sha is derived deterministically from the path so
        # assertions can check it without extra bookkeeping.
        self.files = dict(files)
        self.subdirs = subdirs if subdirs is not None else sorted({
            p.split("/")[2] for p in files if p.startswith("tests/acceptance/")
        })
        self.default_branch_head = "base-sha"
        self.created_branches: list[tuple[str, str]] = []
        self.updated_files: list[tuple[str, str, str]] = []  # (path, branch, content)
        self.created_prs: list[dict] = []
        self.merge_results: dict[int, tuple[bool, str]] = {}
        self._next_pr = 500
        # #2191: per-issue live open/closed override for
        # `get_issues_live_state` — defaults every unlisted issue to "open"
        # so a test exercising `missing_expected_red_warning`'s happy path
        # needs no extra setup.
        self.issue_states: dict[int, str] = {}

    def get_issues_live_state(self, repo: str, numbers: list[int]) -> dict[int, str]:
        return {n: self.issue_states.get(n, "open") for n in numbers}

    def list_repo_subdirs(self, repo: str, path: str, branch: str = "develop") -> list[str]:
        return list(self.subdirs)

    def get_repo_file_with_sha(self, repo: str, path: str, branch: str = "develop") -> tuple[str, str]:
        if path not in self.files:
            raise RuntimeError(f"not found: {path}")
        return self.files[path], f"sha-{path}"

    def list_repo_dir(self, repo: str, path: str, branch: str = "develop") -> list[str]:
        """Filenames directly under *path* — #2543's fragment-dir listing
        (``manifest.d/``) needs this alongside ``list_repo_subdirs``."""
        prefix = path + "/"
        return sorted(
            name[len(prefix):] for name in self.files
            if name.startswith(prefix) and "/" not in name[len(prefix):]
        )

    def get_default_branch_head(self, repo: str, branch: str) -> str:
        return self.default_branch_head

    def create_remote_branch(self, repo: str, branch: str, sha: str) -> bool:
        self.created_branches.append((branch, sha))
        return True

    def update_repo_file(
        self, repo: str, path: str, branch: str, content: str, message: str, *, sha: str,
    ) -> str:
        self.updated_files.append((path, branch, content))
        self.files[path] = content
        return "new-commit-sha"

    def create_pr(self, repo: str, *, base: str, head: str, title: str, body: str) -> dict:
        pr = {"number": self._next_pr, "url": f"https://gh/x/{self._next_pr}"}
        self._next_pr += 1
        self.created_prs.append({"base": base, "head": head, "title": title, "body": body, **pr})
        return pr

    def merge_pr(self, repo: str, number: int, method: str = "rebase") -> tuple[bool, str]:
        return self.merge_results.get(number, (True, "merged"))


MS01_MANIFEST = (
    "tests:\n  ms01::a: 944\n  ms01::b: 944\n"
    "expected_red:\n  944:\n    - ms01::a\n"
)


class TestFindMsManifestForIssueViaApi:
    def test_finds_the_manifest_mapping_the_issue(self) -> None:
        ops = _FakeApiGhOps({"tests/acceptance/ms01/manifest.yml": MS01_MANIFEST})
        found = find_ms_manifest_for_issue_via_api("acme/x", "main", 944, gh_ops=ops)
        assert found is not None
        path, text, blob_sha, data = found
        assert path == "tests/acceptance/ms01/manifest.yml"
        assert data.expected_red == {944: frozenset({"ms01::a"})}

    def test_none_when_no_manifest_maps_the_issue(self) -> None:
        ops = _FakeApiGhOps({"tests/acceptance/ms01/manifest.yml": MS01_MANIFEST})
        assert find_ms_manifest_for_issue_via_api("acme/x", "main", 12345, gh_ops=ops) is None

    def test_none_when_gh_ops_lacks_the_api_methods(self) -> None:
        class Bare:
            pass

        assert find_ms_manifest_for_issue_via_api("acme/x", "main", 944, gh_ops=Bare()) is None

    def test_finds_the_issue_via_its_own_fragment(self) -> None:
        """#2543: an issue whose data lives ONLY in its own
        manifest.d/<issue>.yml fragment (no legacy manifest.yml at all) is
        still found — a targeted fetch by exact, predictable filename."""
        ops = _FakeApiGhOps({
            "tests/acceptance/ms01/manifest.d/944.yml": MS01_MANIFEST,
        })
        found = find_ms_manifest_for_issue_via_api("acme/x", "main", 944, gh_ops=ops)
        assert found is not None
        path, text, blob_sha, data = found
        assert path == "tests/acceptance/ms01/manifest.d/944.yml"
        assert data.expected_red == {944: frozenset({"ms01::a"})}

    def test_fragment_and_legacy_file_coexist_each_found_by_its_own_issue(self) -> None:
        """Sibling issues in the same ms-dir: one's data is in the legacy
        manifest.yml, the other's is in its own fragment — each is found
        independently, proving the two never had to collide on one file."""
        ops = _FakeApiGhOps({
            "tests/acceptance/ms01/manifest.yml": "tests:\n  ms01::legacy: 900\n",
            "tests/acceptance/ms01/manifest.d/944.yml": MS01_MANIFEST,
        })
        found_frag = find_ms_manifest_for_issue_via_api("acme/x", "main", 944, gh_ops=ops)
        assert found_frag is not None and found_frag[0].endswith("manifest.d/944.yml")
        found_legacy = find_ms_manifest_for_issue_via_api("acme/x", "main", 900, gh_ops=ops)
        assert found_legacy is not None and found_legacy[0].endswith("manifest.yml")
        assert "manifest.d" not in found_legacy[0]


class TestMissingExpectedRedWarning:
    """#2191: the gate half — flags the exact "manifest maps ids to an open
    issue with no expected_red" signature an unwritten registry produces,
    at slice-PR-open time."""

    def test_warns_when_open_issue_has_tests_but_no_expected_red(self) -> None:
        ops = _FakeApiGhOps({
            "tests/acceptance/ms01/manifest.yml": "tests:\n  ms01::a: 944\n  ms01::b: 944\n",
        })
        warning = missing_expected_red_warning("acme/x", "main", 944, gh_ops=ops)
        assert warning is not None
        assert "#944" in warning
        assert "ms01::a" in warning and "ms01::b" in warning
        assert "expected_red" in warning

    def test_none_when_expected_red_already_recorded(self) -> None:
        ops = _FakeApiGhOps({"tests/acceptance/ms01/manifest.yml": MS01_MANIFEST})
        assert missing_expected_red_warning("acme/x", "main", 944, gh_ops=ops) is None

    def test_none_when_issue_not_referenced_by_any_manifest(self) -> None:
        ops = _FakeApiGhOps({"tests/acceptance/ms01/manifest.yml": MS01_MANIFEST})
        assert missing_expected_red_warning("acme/x", "main", 12345, gh_ops=ops) is None

    def test_none_when_issue_is_closed(self) -> None:
        ops = _FakeApiGhOps({
            "tests/acceptance/ms01/manifest.yml": "tests:\n  ms01::a: 944\n",
        })
        ops.issue_states[944] = "closed"
        assert missing_expected_red_warning("acme/x", "main", 944, gh_ops=ops) is None

    def test_none_when_gh_ops_lacks_live_state_lookup(self) -> None:
        """Fail-open: a gh_ops stub that can find the manifest but doesn't
        support the live-state lookup must not warn — this check is
        advisory, never a false positive from an incomplete stub/older
        gh_ops."""

        class NoLiveState:
            def __init__(self, inner: _FakeApiGhOps):
                self._inner = inner

            def list_repo_subdirs(self, *a, **kw):
                return self._inner.list_repo_subdirs(*a, **kw)

            def get_repo_file_with_sha(self, *a, **kw):
                return self._inner.get_repo_file_with_sha(*a, **kw)

        inner = _FakeApiGhOps({"tests/acceptance/ms01/manifest.yml": "tests:\n  ms01::a: 944\n"})
        assert missing_expected_red_warning("acme/x", "main", 944, gh_ops=NoLiveState(inner)) is None

    def test_none_when_gh_ops_lacks_api_methods_at_all(self) -> None:
        class Bare:
            pass

        assert missing_expected_red_warning("acme/x", "main", 944, gh_ops=Bare()) is None


class TestListExpectedRedViaApi:
    def test_merges_across_ms_dirs(self) -> None:
        ops = _FakeApiGhOps({
            "tests/acceptance/ms01/manifest.yml": MS01_MANIFEST,
            "tests/acceptance/ms02/manifest.yml": (
                "tests:\n  ms02::z: 945\nexpected_red:\n  945:\n    - ms02::z\n"
            ),
        })
        result = list_expected_red_via_api("acme/x", "main", gh_ops=ops)
        assert result == {
            "ms01": {944: frozenset({"ms01::a"})},
            "ms02": {945: frozenset({"ms02::z"})},
        }

    def test_empty_when_nothing_expected_red(self) -> None:
        ops = _FakeApiGhOps({"tests/acceptance/ms01/manifest.yml": "tests:\n  a: 1\n"})
        assert list_expected_red_via_api("acme/x", "main", gh_ops=ops) == {}

    def test_merges_fragments_alongside_legacy_file(self) -> None:
        """#2543: expected_red split across per-issue manifest.d/ fragments
        (enumerated via list_repo_dir) merges into the same per-ms-dir view
        a legacy shared manifest.yml would have produced."""
        ops = _FakeApiGhOps({
            "tests/acceptance/ms01/manifest.yml": MS01_MANIFEST,
            "tests/acceptance/ms01/manifest.d/945.yml": (
                "expected_red:\n  945:\n    - ms01::z\n"
            ),
        })
        result = list_expected_red_via_api("acme/x", "main", gh_ops=ops)
        assert result == {
            "ms01": {944: frozenset({"ms01::a"}), 945: frozenset({"ms01::z"})},
        }

    def test_fragments_only_no_legacy_file(self) -> None:
        ops = _FakeApiGhOps({
            "tests/acceptance/ms01/manifest.d/944.yml": (
                "expected_red:\n  944:\n    - ms01::a\n"
            ),
        })
        assert list_expected_red_via_api("acme/x", "main", gh_ops=ops) == {
            "ms01": {944: frozenset({"ms01::a"})},
        }

    def test_degrades_gracefully_without_list_repo_dir(self) -> None:
        """A gh_ops that supports the legacy surface but not list_repo_dir
        (an older stub) still reports the legacy file's entries — fragments
        are just invisible to it, never a hard failure."""

        class NoListDir:
            def __init__(self, inner: _FakeApiGhOps):
                self._inner = inner

            def list_repo_subdirs(self, *a, **kw):
                return self._inner.list_repo_subdirs(*a, **kw)

            def get_repo_file_with_sha(self, *a, **kw):
                return self._inner.get_repo_file_with_sha(*a, **kw)

        inner = _FakeApiGhOps({"tests/acceptance/ms01/manifest.yml": MS01_MANIFEST})
        result = list_expected_red_via_api("acme/x", "main", gh_ops=NoListDir(inner))
        assert result == {"ms01": {944: frozenset({"ms01::a"})}}


class TestClearExpectedRedViaPr:
    """#2164 review fix: clearing now goes through a real PR
    (create_pr + merge_pr), fired post-merge — never a raw push at
    record-time. See coord.merge_queue's TestExpectedRedClearOnMerge for
    the caller side."""

    def test_happy_path_opens_and_merges_a_clearing_pr(self) -> None:
        ops = _FakeApiGhOps({"tests/acceptance/ms01/manifest.yml": MS01_MANIFEST})
        msg = clear_expected_red_via_pr("acme/x", "coord-tui", "main", 944, gh_ops=ops)
        assert "cleared expected_red for #944: ms01::a" in msg
        assert ops.created_branches  # a throwaway branch was created off the default tip
        assert ops.created_branches[0][1] == "base-sha"
        [update] = ops.updated_files
        path, branch, content = update
        assert path == "tests/acceptance/ms01/manifest.yml"
        assert "expected_red" not in content
        assert ops.created_prs and ops.created_prs[0]["base"] == "main"

    def test_no_entries_is_a_no_op(self) -> None:
        ops = _FakeApiGhOps({"tests/acceptance/ms01/manifest.yml": "tests:\n  a: 944\n"})
        msg = clear_expected_red_via_pr("acme/x", "coord-tui", "main", 944, gh_ops=ops)
        assert "no expected_red" in msg
        assert not ops.created_prs

    def test_json_manifest_is_declined_with_an_explicit_warning(self) -> None:
        import json as _json

        ops = _FakeApiGhOps({
            "tests/acceptance/ms01/manifest.json": _json.dumps(
                {"tests": {"ms01::a": 944}, "expected_red": {"944": ["ms01::a"]}}
            ),
        })
        msg = clear_expected_red_via_pr("acme/x", "coord-tui", "main", 944, gh_ops=ops)
        assert "warning" in msg and "JSON" in msg
        assert not ops.created_prs

    def test_pr_that_does_not_merge_reports_a_retry_warning(self) -> None:
        ops = _FakeApiGhOps({"tests/acceptance/ms01/manifest.yml": MS01_MANIFEST})
        ops.merge_results[500] = (False, "required check pending")
        msg = clear_expected_red_via_pr("acme/x", "coord-tui", "main", 944, gh_ops=ops)
        assert "did not merge" in msg
        assert "retry" in msg

    def test_no_manifest_found_at_all(self) -> None:
        ops = _FakeApiGhOps({})
        msg = clear_expected_red_via_pr("acme/x", "coord-tui", "main", 944, gh_ops=ops)
        assert "no expected_red entries found" in msg

    def test_no_match_in_text_surgery_is_reported_as_a_warning(self) -> None:
        """#2296: when ids were resolved from the parsed manifest but the
        text-surgery pass finds nothing to remove (e.g. a flow-style
        `944: [ms01::a]` entry the block-style-only text surgery doesn't
        understand — see `_issue_header_re`'s docstring), that is a defect,
        not a benign no-op, and must be `warning:`-prefixed so
        `classify_expected_red_clear_result` reads it as "failed" rather
        than a clean sweep."""
        ops = _FakeApiGhOps({
            "tests/acceptance/ms01/manifest.yml": (
                "tests:\n  ms01::a: 944\n"
                "expected_red:\n  944: [ms01::a]\n"
            ),
        })
        msg = clear_expected_red_via_pr("acme/x", "coord-tui", "main", 944, gh_ops=ops)
        assert msg.startswith("warning:")
        assert "nothing matched" in msg
        assert classify_expected_red_clear_result(msg) == "failed"
        assert not ops.created_prs

    def test_clears_a_per_issue_fragment_file(self) -> None:
        """#2543: clearing an issue whose expected_red lives in its OWN
        manifest.d/<issue>.yml fragment edits exactly that file, and the
        derived throwaway branch name still keys off the real ms-dir (not
        "manifest.d", the fragment's immediate parent)."""
        ops = _FakeApiGhOps({
            "tests/acceptance/ms01/manifest.d/944.yml": MS01_MANIFEST,
        })
        msg = clear_expected_red_via_pr("acme/x", "coord-tui", "main", 944, gh_ops=ops)
        assert "cleared expected_red for #944: ms01::a" in msg
        [update] = ops.updated_files
        path, branch, content = update
        assert path == "tests/acceptance/ms01/manifest.d/944.yml"
        assert "expected_red" not in content
        assert branch.startswith("coord/clear-expected-red-944-ms01")


class _MultiRootApiGhOps(_FakeApiGhOps):
    """#2896 review: a `_FakeApiGhOps` whose `list_repo_subdirs` actually
    honours the *path* it is asked about, instead of returning one fixed
    repo-root listing regardless. Without that, a test cannot tell a sweep
    that searches the relocated (entrypoint-linked) root apart from one
    that only ever looks at `tests/acceptance/` — which is exactly the bug
    these tests exist to pin."""

    def list_repo_subdirs(self, repo: str, path: str, branch: str = "develop") -> list[str]:
        prefix = path.rstrip("/") + "/"
        out = {
            name[len(prefix):].split("/")[0]
            for name in self.files
            if name.startswith(prefix) and "/" in name[len(prefix):]
        }
        if not out:
            raise RuntimeError(f"not found: {path}")
        return sorted(out)


# The two roots a routed repo with an entrypoint-linked driver declares —
# `coord.config.AcceptanceConfig.acceptance_search_roots` output shape.
BOTH_ROOTS = ["tests/acceptance/", "tui/tests/acceptance/"]

MS65_MANIFEST = (
    "tests:\n  ms65::a: 2282\n  ms65::b: 2282\n"
    "expected_red:\n  2282:\n    - ms65::a\n"
)


class TestSearchRootsForRepo:
    """#2896 review: the one place the "…or fall back to the legacy
    repo-root tree" rule is spelled, so every API-only sweep agrees."""

    def test_no_config_falls_back_to_the_legacy_root(self) -> None:
        assert search_roots_for_repo(None, None) == ["tests/acceptance/"]
        assert search_roots_for_repo(None, "claude-coordinator") == ["tests/acceptance/"]

    def test_driverless_repo_falls_back_to_the_legacy_root(self) -> None:
        cfg = Config(repos=[Repo(name="api", github="acme/api")], machines=[])
        assert search_roots_for_repo(cfg, "api") == ["tests/acceptance/"]

    def test_entrypoint_linked_driver_adds_its_sibling_root(self) -> None:
        cfg = Config(
            repos=[Repo(name="claude-coordinator", github="acme/cc")],
            machines=[],
            acceptance=AcceptanceConfig(drivers={
                "claude-coordinator": AcceptanceDriverConfig(routes=[
                    AcceptanceDriverConfig(
                        match="coord/**", kind="cli-pytest", run="pytest",
                    ),
                    AcceptanceDriverConfig(
                        match="tui/**", kind="tui-tuidriver", run="cargo test",
                        entrypoint="tui/tests/acceptance.rs",
                    ),
                ]),
            }),
        )
        assert search_roots_for_repo(cfg, "claude-coordinator") == BOTH_ROOTS


class TestApiSweepsSearchRelocatedRoots:
    """#2896 review (blocking): the GitHub-API-only `expected_red` sweep
    hardcoded the repo-root `tests/acceptance/`, so every RELOCATED
    (entrypoint-linked) milestone — ms-65/ms-67 here, whose slices now live
    at `tui/tests/acceptance/ms-NN/` — went dark to it. That silently broke
    three things at once: the merge queue's post-merge #2164 trust-gate
    clear (which read "nothing recorded" as "never in scope" and returned
    bare `None`, the exact #2199 regression), `coord acceptance
    expected-red`'s listing (#2164's invisible-debt detector), and
    `clear_expected_red_via_pr` (which reported "no expected_red entries
    found" and never opened the clearing PR)."""

    def test_find_ms_manifest_misses_a_relocated_milestone_without_roots(self) -> None:
        """The bug, pinned: default (legacy single-root) behaviour is
        preserved exactly — and is wrong for a relocated milestone."""
        ops = _MultiRootApiGhOps({
            "tui/tests/acceptance/ms-65/manifest.yml": MS65_MANIFEST,
        })
        assert find_ms_manifest_for_issue_via_api("acme/x", "main", 2282, gh_ops=ops) is None

    def test_find_ms_manifest_finds_a_relocated_milestone(self) -> None:
        ops = _MultiRootApiGhOps({
            "tui/tests/acceptance/ms-65/manifest.yml": MS65_MANIFEST,
        })
        found = find_ms_manifest_for_issue_via_api(
            "acme/x", "main", 2282, gh_ops=ops, search_roots=BOTH_ROOTS,
        )
        assert found is not None
        path, _text, _sha, data = found
        assert path == "tui/tests/acceptance/ms-65/manifest.yml"
        assert data.expected_red == {2282: frozenset({"ms65::a"})}

    def test_find_ms_manifest_finds_a_relocated_fragment(self) -> None:
        """#2543 fragments live under the relocated root too."""
        ops = _MultiRootApiGhOps({
            "tui/tests/acceptance/ms-65/manifest.d/2282.yml": MS65_MANIFEST,
        })
        found = find_ms_manifest_for_issue_via_api(
            "acme/x", "main", 2282, gh_ops=ops, search_roots=BOTH_ROOTS,
        )
        assert found is not None
        assert found[0] == "tui/tests/acceptance/ms-65/manifest.d/2282.yml"

    def test_legacy_root_still_found_when_both_roots_searched(self) -> None:
        """A directory-discovered driver's slices (ms-37 here) never moved —
        adding roots must not cost the legacy tree its own lookups."""
        ops = _MultiRootApiGhOps({
            "tests/acceptance/ms01/manifest.yml": MS01_MANIFEST,
            "tui/tests/acceptance/ms-65/manifest.yml": MS65_MANIFEST,
        })
        legacy = find_ms_manifest_for_issue_via_api(
            "acme/x", "main", 944, gh_ops=ops, search_roots=BOTH_ROOTS,
        )
        relocated = find_ms_manifest_for_issue_via_api(
            "acme/x", "main", 2282, gh_ops=ops, search_roots=BOTH_ROOTS,
        )
        assert legacy is not None and legacy[0] == "tests/acceptance/ms01/manifest.yml"
        assert relocated is not None
        assert relocated[0] == "tui/tests/acceptance/ms-65/manifest.yml"

    def test_a_missing_root_does_not_abort_the_sweep(self) -> None:
        """A root that doesn't exist on this branch (a repo with no
        `tests/acceptance/` at all) must skip to the next root, not read as
        "nothing found anywhere" — the single-root fail-soft posture,
        preserved across N roots."""
        ops = _MultiRootApiGhOps({
            "tui/tests/acceptance/ms-65/manifest.yml": MS65_MANIFEST,
        })
        found = find_ms_manifest_for_issue_via_api(
            "acme/x", "main", 2282, gh_ops=ops, search_roots=BOTH_ROOTS,
        )
        assert found is not None

    def test_list_expected_red_unions_both_roots(self) -> None:
        ops = _MultiRootApiGhOps({
            "tests/acceptance/ms01/manifest.yml": MS01_MANIFEST,
            "tui/tests/acceptance/ms-65/manifest.yml": MS65_MANIFEST,
        })
        assert list_expected_red_via_api("acme/x", "main", gh_ops=ops) == {
            "ms01": {944: frozenset({"ms01::a"})},
        }
        assert list_expected_red_via_api(
            "acme/x", "main", gh_ops=ops, search_roots=BOTH_ROOTS,
        ) == {
            "ms01": {944: frozenset({"ms01::a"})},
            "ms-65": {2282: frozenset({"ms65::a"})},
        }

    def test_missing_expected_red_warning_sees_a_relocated_milestone(self) -> None:
        """#2191's writer/gate seam must not go silent on a relocated
        slice — silence there is indistinguishable from "no slice
        authored"."""
        ops = _MultiRootApiGhOps({
            "tui/tests/acceptance/ms-65/manifest.yml": "tests:\n  ms65::a: 2282\n",
        })
        assert missing_expected_red_warning("acme/x", "main", 2282, gh_ops=ops) is None
        warning = missing_expected_red_warning(
            "acme/x", "main", 2282, gh_ops=ops, search_roots=BOTH_ROOTS,
        )
        assert warning is not None
        assert "tui/tests/acceptance/ms-65/manifest.yml" in warning

    def test_clear_expected_red_opens_the_pr_for_a_relocated_milestone(self) -> None:
        ops = _MultiRootApiGhOps({
            "tui/tests/acceptance/ms-65/manifest.yml": MS65_MANIFEST,
        })
        assert clear_expected_red_via_pr(
            "acme/x", "coord-tui", "main", 2282, gh_ops=ops,
        ) == "no expected_red entries found for this issue"

        msg = clear_expected_red_via_pr(
            "acme/x", "coord-tui", "main", 2282, gh_ops=ops, search_roots=BOTH_ROOTS,
        )
        assert msg.startswith("cleared expected_red for #2282")
        assert ops.created_prs, "a clearing PR must actually be opened"
        path, _branch, content = ops.updated_files[-1]
        assert path == "tui/tests/acceptance/ms-65/manifest.yml"
        assert "expected_red" not in content


class TestClassifyExpectedRedClearResult:
    """#2266 review (blocking findings 1 & 2): the single shared
    classifier both `coord.merge_queue._maybe_clear_expected_red` and
    `coord.commands.acceptance._clear_stuck_expected_red` call, instead of
    each independently re-deriving "did this succeed?" via its own
    `msg.startswith("cleared expected_red")` check. Exercised here against
    every message shape `clear_expected_red_via_pr` actually returns (see
    `TestClearExpectedRedViaPr` above), so the two can never silently
    desync on a message-wording change."""

    def test_cleared(self) -> None:
        assert classify_expected_red_clear_result(
            "cleared expected_red for #944: ms01::a (PR #501)"
        ) == "cleared"

    def test_no_manifest_found_is_a_no_op_not_a_failure(self) -> None:
        assert classify_expected_red_clear_result(
            "no expected_red entries found for this issue"
        ) == "no_op"

    def test_no_entries_for_issue_is_a_no_op_not_a_failure(self) -> None:
        """#2266 review blocking finding 1: this is the *common* case for
        an ordinary oracle-loop merge whose issue was never part of a
        deliberately-red slice — it must never classify as a failure."""
        assert classify_expected_red_clear_result(
            "no expected_red entries for this issue"
        ) == "no_op"

    def test_pr_that_did_not_merge_is_pending_retry_not_a_hard_failure(self) -> None:
        assert classify_expected_red_clear_result(
            "expected_red clear PR #501 opened but did not merge (branch "
            "protection / required checks pending?) — will retry on the "
            "next merge (required check pending)"
        ) == "pending_retry"

    def test_json_manifest_warning_is_a_failure(self) -> None:
        assert classify_expected_red_clear_result(
            "warning: tests/acceptance/ms01/manifest.json is a JSON "
            "manifest — automatic expected_red clearing only supports "
            "YAML manifests today; clear ms01::a by hand"
        ) == "failed"

    def test_generic_warning_is_a_failure(self) -> None:
        assert classify_expected_red_clear_result(
            "warning: could not open expected_red clear PR: boom"
        ) == "failed"

    def test_unchanged_text_is_a_failure(self) -> None:
        assert classify_expected_red_clear_result(
            "warning: expected_red text unchanged (nothing matched)"
        ) == "failed"


class TestOracleLoopContractBlock:
    def test_empty_when_issue_not_authored(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        assert oracle_loop_contract_block(root, "api", 945) == ""

    def test_empty_on_malformed_manifest(self, tmp_path: Path) -> None:
        # Fail-soft (#603-style): a manifest read hiccup must never blow up
        # the dispatch hot path — it degrades to "no block" instead.
        root = tmp_path / "tests" / "acceptance"
        (root / "ms01").mkdir(parents=True)
        (root / "ms01" / "manifest.yml").write_text("tests: [not, a, mapping\n")
        assert oracle_loop_contract_block(root, "api", 945) == ""

    def test_block_names_contract_path_and_run_command(self, tmp_path: Path) -> None:
        root = tmp_path / "tests" / "acceptance"
        (root / "ms25").mkdir(parents=True)
        (root / "ms25" / "manifest.yml").write_text("tests:\n  ms25::a: 945\n")
        block = oracle_loop_contract_block(root, "api", 945)
        assert block.startswith("## 🔒 Oracle-loop acceptance contract")
        assert "tests/acceptance/ms25/contract.md" in block
        assert "coord acceptance run --repo api --issue 945" in block
        assert "tests/acceptance/**" in block
        assert "STUCK:" in block
        # #846: the contract points a churning worker at `coord acceptance
        # stall` (in addition to a STUCK: line for the interactive log).
        assert "coord acceptance stall --repo api --issue 945" in block

    def test_block_points_at_the_mocks_dir_and_says_satisfy_not_edit(
        self, tmp_path: Path
    ) -> None:
        """#1542: for a web slice the `.html` mocks under `mocks/` are part
        of the sealed contract, not just `contract.md` — the worker briefing
        must say so plainly, and must not soften "may not edit
        tests/acceptance/**" into something that reads as "edit the
        assertions to match the app."""
        root = tmp_path / "tests" / "acceptance"
        (root / "ms25").mkdir(parents=True)
        (root / "ms25" / "manifest.yml").write_text("tests:\n  ms25::a: 945\n")
        block = oracle_loop_contract_block(root, "api", 945)
        assert "tests/acceptance/ms25/mocks/" in block
        assert "must satisfy" in block
        assert "not the other way around" in block

    def test_acceptance_dirname_names_a_relocated_slices_paths(
        self, tmp_path: Path
    ) -> None:
        """#2896: a caller driving an entrypoint-linked driver's relocated
        acceptance tree (e.g. `tui/tests/acceptance/`) passes BOTH the local
        root to scan (here `tmp_path / "tui/tests/acceptance"`) and the
        matching repo-relative name to print (`acceptance_dirname`) — the
        printed contract/mocks paths and the "may not edit" line must name
        the ACTUAL location, not the shared repo-root default."""
        root = tmp_path / "tui" / "tests" / "acceptance"
        (root / "ms65").mkdir(parents=True)
        (root / "ms65" / "manifest.yml").write_text("tests:\n  ms65::a: 2282\n")
        block = oracle_loop_contract_block(
            root, "coord-tui", 2282, acceptance_dirname="tui/tests/acceptance/",
        )
        assert "tui/tests/acceptance/ms65/contract.md" in block
        assert "tui/tests/acceptance/ms65/mocks/" in block
        assert "edit `tui/tests/acceptance/**`" in block
        # Never names the (wrong, unrelated) repo-root default.
        assert "`tests/acceptance/" not in block


class TestGateAContractCandidates:
    """#2896: a bare milestone number doesn't say which acceptance search
    root its contract lives under — gate_a_contract_candidates tries every
    root the repo declares instead of guessing the legacy repo-root one."""

    def _cfg_with_entrypoint(self) -> Config:
        return Config(
            repos=[Repo(name="claude-coordinator", github="acme/claude-coordinator")],
            machines=[],
            acceptance=AcceptanceConfig(drivers={
                "claude-coordinator": AcceptanceDriverConfig(routes=[
                    AcceptanceDriverConfig(match="coord/**", kind="cli-pytest", run="pytest"),
                    AcceptanceDriverConfig(
                        match="tui/**", kind="tui-tuidriver", run="cargo test",
                        entrypoint="tui/tests/acceptance.rs",
                    ),
                ]),
            }),
        )

    def test_single_candidate_when_no_entrypoint_declared(self) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(kind="cli-pytest", run="pytest"),
            }),
        )
        assert gate_a_contract_candidates(cfg, "api", 9) == [
            "tests/acceptance/ms-9/contract.md",
        ]

    def test_two_candidates_when_repo_has_an_entrypoint_linked_route(self) -> None:
        cfg = self._cfg_with_entrypoint()
        assert gate_a_contract_candidates(cfg, "claude-coordinator", 65) == [
            "tests/acceptance/ms-65/contract.md",
            "tui/tests/acceptance/ms-65/contract.md",
        ]

    def test_falls_back_to_legacy_single_candidate_for_unconfigured_repo(self) -> None:
        cfg = Config(repos=[Repo(name="api", github="acme/api")], machines=[])
        assert gate_a_contract_candidates(cfg, "api", 9) == [
            gate_a_contract_path(9),
        ]


class TestIssueDirnameAndBugContractPath:
    """#1964 (docs/TEST_FIRST_BUG_LANE.md): the bug lane's single-issue
    counterpart to ms_dirname/gate_a_contract_path — no milestone number
    anywhere in the name."""

    def test_issue_dirname(self) -> None:
        assert issue_dirname(1234) == "issue-1234"

    def test_bug_contract_path(self) -> None:
        assert bug_contract_path(1234) == "tests/acceptance/issue-1234/contract.md"


class TestBugLaneNeedsNoMilestone:
    """#1964: end-to-end proof that a hand-authored `issue-NN/` slice —
    with no `ms-NN/` directory anywhere in the tree, and no milestone
    involved at any step — is discovered, scoped, and injected into the
    worker briefing by the exact same machinery an `ms-NN/` slice uses.
    This is the acceptance bar for "no milestone ceremony required"."""

    def test_manifest_and_contract_block_work_with_only_an_issue_dir(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "tests" / "acceptance"
        bug_dir = root / issue_dirname(1234)
        bug_dir.mkdir(parents=True)
        (bug_dir / "manifest.yml").write_text("tests:\n  issue_1234::popup_border: 1234\n")

        assert ms_dir_for_issue(root, 1234) == "issue-1234"
        manifest = load_manifest(root)
        assert manifest == {"issue_1234::popup_border": 1234}

        block = oracle_loop_contract_block(root, "vimcode", 1234)
        assert bug_contract_path(1234) in block
        assert "coord acceptance run --repo vimcode --issue 1234" in block
        # No ms-* dir exists anywhere — confirm the fixture itself proves the
        # negative, not just that the assertions above happened to pass.
        assert not list(root.glob("ms-*"))


class TestAcceptanceCapabilityGap:
    """#966: cheap detection mirroring `coord.smoke.pick_smoke_machine`'s
    candidate filter — no remote-exec plumbing, just "is this the wrong
    host?" so callers can fail loudly instead of running on hardware that
    can't actually support the driver."""

    @staticmethod
    def _config(*, here_caps: list[str], other_caps: list[str]) -> Config:
        return Config(
            repos=[Repo(name="webapp", github="acme/webapp")],
            machines=[
                Machine(
                    name="here", host="here.tail", capabilities=here_caps,
                    repos=["webapp"],
                ),
                Machine(
                    name="other", host="other.tail", capabilities=other_caps,
                    repos=["webapp"],
                ),
            ],
        )

    def test_no_capability_required_is_never_a_gap(self, monkeypatch) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "here")
        cfg = self._config(here_caps=[], other_caps=["browser"])
        assert acceptance_capability_gap("", "webapp", cfg) is None

    def test_local_host_already_has_capability_no_gap(self, monkeypatch) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "here")
        cfg = self._config(here_caps=["browser"], other_caps=[])
        assert acceptance_capability_gap("browser", "webapp", cfg) is None

    def test_local_host_missing_capability_returns_other_machine(self, monkeypatch) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "here")
        cfg = self._config(here_caps=[], other_caps=["browser"])
        gap = acceptance_capability_gap("browser", "webapp", cfg)
        assert gap is not None
        assert gap.name == "other"

    def test_no_other_machine_has_it_either_no_gap(self, monkeypatch) -> None:
        # Nothing to route to — failing wouldn't be actionable.
        monkeypatch.setattr("socket.gethostname", lambda: "here")
        cfg = self._config(here_caps=[], other_caps=[])
        assert acceptance_capability_gap("browser", "webapp", cfg) is None

    def test_unrecognized_host_gets_benefit_of_the_doubt(self, monkeypatch) -> None:
        # This process's hostname doesn't match any configured machine —
        # could be a dev box outside the fleet with everything installed.
        monkeypatch.setattr("socket.gethostname", lambda: "nowhere")
        cfg = self._config(here_caps=[], other_caps=["browser"])
        assert acceptance_capability_gap("browser", "webapp", cfg) is None

    def test_other_machine_without_repo_access_is_not_a_candidate(self, monkeypatch) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "here")
        cfg = Config(
            repos=[Repo(name="webapp", github="acme/webapp")],
            machines=[
                Machine(name="here", host="here.tail", capabilities=[], repos=["webapp"]),
                Machine(
                    name="other", host="other.tail", capabilities=["browser"],
                    repos=["some-other-repo"],
                ),
            ],
        )
        assert acceptance_capability_gap("browser", "webapp", cfg) is None


class TestWorkedExampleWebMock:
    """#1542: `tests/acceptance/ms-example/mocks/home-active.html` is the
    committed worked example the "next test-author copies a real file, not
    a description" — this pins the acceptance criteria that make it a valid
    `web-playwright` mock rather than just some HTML file: self-contained
    (no external assets it can't render without), carries its own CSS, and
    exposes hooks (`data-testid`) a test-author would actually assert
    against. `ms-example` is deliberately NOT a real milestone number so it
    is inert to every milestone-scanning code path (`load_manifest`,
    `ms_dir_for_issue`, `resolve_for_path`'s GitHub-dir listing) — this test
    reads it straight off disk instead."""

    MOCK_PATH = (
        Path(__file__).resolve().parent.parent
        / "tests" / "acceptance" / "ms-example" / "mocks" / "home-active.html"
    )

    def test_mock_file_exists(self) -> None:
        assert self.MOCK_PATH.is_file(), f"missing worked example: {self.MOCK_PATH}"

    def test_mock_is_self_contained_no_external_assets(self) -> None:
        html = self.MOCK_PATH.read_text()
        assert "<link" not in html.lower(), "no external stylesheet — CSS must be inline"
        assert "src=\"http" not in html.lower()
        assert "src='http" not in html.lower()

    def test_mock_carries_its_own_inline_css(self) -> None:
        html = self.MOCK_PATH.read_text()
        assert "<style>" in html

    def test_mock_exposes_testable_hooks(self) -> None:
        html = self.MOCK_PATH.read_text()
        assert 'data-testid="pipeline-card"' in html
        assert 'role="tab"' in html
        assert 'aria-selected="true"' in html

    def test_mock_extension_resolves_to_web_playwright(self) -> None:
        assert MOCK_EXT_TO_DRIVER_KIND[self.MOCK_PATH.suffix] == "web-playwright"


class TestMockExtToDriverKindRegistry:
    """#1542: the single source of truth for mock-suffix -> driver-kind
    resolution — every consumer (`resolve_for_path`, and the mock-author /
    test-author briefings' human-facing descriptions) must agree with this
    table rather than re-deriving it."""

    def test_html_maps_to_web_playwright(self) -> None:
        assert MOCK_EXT_TO_DRIVER_KIND[".html"] == "web-playwright"

    def test_screen_and_out_are_unchanged(self) -> None:
        assert MOCK_EXT_TO_DRIVER_KIND[".screen"] == "tui-tuidriver"
        assert MOCK_EXT_TO_DRIVER_KIND[".out"] == "cli-pytest"


class TestResolveForPath:
    """#1453 review finding 1: the ONE place ``--for-path`` is derived from
    a milestone's Gate-A mock kind (``*.screen`` -> ``tui-tuidriver``,
    ``*.out`` -> ``cli-pytest``, docs/ORACLE_LOOP.md) — shared by
    ``coord/drive.py``'s JIT-authoring dispatch (and, per the pinned #1453
    review guidance, #1460's eventual TUI-menu equivalent)."""

    @staticmethod
    def _routed_config() -> Config:
        return Config(
            repos=[Repo(name="claude-coordinator", github="john/claude-coordinator")],
            machines=[],
            acceptance=AcceptanceConfig(
                drivers={
                    "claude-coordinator": AcceptanceDriverConfig(
                        routes=[
                            AcceptanceDriverConfig(
                                match="coord/**", kind="cli-pytest", run="pytest",
                            ),
                            AcceptanceDriverConfig(
                                match="tui/**", kind="tui-tuidriver", run="cargo test",
                            ),
                            AcceptanceDriverConfig(
                                match="coord/dashboard/webapp/**", kind="web-playwright",
                                run="npx playwright test",
                            ),
                        ]
                    )
                }
            ),
        )

    def test_repo_with_no_driver_at_all_returns_none(self) -> None:
        cfg = Config(repos=[Repo(name="api", github="acme/api")], machines=[])
        assert resolve_for_path(cfg, cfg.repo("api"), 37) is None

    def test_unrouted_flat_driver_returns_none_without_listing_anything(self) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[],
            acceptance=AcceptanceConfig(
                drivers={"api": AcceptanceDriverConfig(kind="cli-pytest", run="pytest")}
            ),
        )
        calls: list[tuple] = []
        result = resolve_for_path(
            cfg, cfg.repo("api"), 37,
            list_mock_dir=lambda *a: calls.append(a) or (),
        )
        assert result is None
        assert calls == []

    def test_screen_mocks_resolve_to_the_tui_tuidriver_route(self) -> None:
        cfg = self._routed_config()
        result = resolve_for_path(
            cfg, cfg.repo("claude-coordinator"), 38,
            list_mock_dir=lambda *a: ("plans-base.screen", "plans-detail.screen"),
        )
        assert result == "tui/**"

    def test_out_mocks_resolve_to_the_cli_pytest_route(self) -> None:
        cfg = self._routed_config()
        result = resolve_for_path(
            cfg, cfg.repo("claude-coordinator"), 37,
            list_mock_dir=lambda *a: ("usage_by_issue.out",),
        )
        assert result == "coord/**"

    def test_html_mocks_resolve_to_the_web_playwright_route(self) -> None:
        """#1542: the hand-authored HTML wireframe shape (docs/ORACLE_LOOP.md)
        resolves the same way `.screen`/`.out` already do — the whole point
        of registering `.html` in `MOCK_EXT_TO_DRIVER_KIND` is that this
        derivation needs no kind-specific code path."""
        cfg = self._routed_config()
        result = resolve_for_path(
            cfg, cfg.repo("claude-coordinator"), 42,
            list_mock_dir=lambda *a: ("home-active.html", "home-empty.html"),
        )
        assert result == "coord/dashboard/webapp/**"

    def test_passes_repo_github_mocks_path_and_default_branch_to_the_lister(self) -> None:
        cfg = self._routed_config()
        calls: list[tuple] = []
        resolve_for_path(
            cfg, cfg.repo("claude-coordinator"), 38,
            list_mock_dir=lambda *a: calls.append(a) or ("x.screen",),
        )
        assert calls == [
            ("john/claude-coordinator", "tests/acceptance/ms-38/mocks", "main"),
        ]

    def test_unrecognized_extensions_are_ignored_not_fatal(self) -> None:
        cfg = self._routed_config()
        result = resolve_for_path(
            cfg, cfg.repo("claude-coordinator"), 38,
            list_mock_dir=lambda *a: ("README.md", "a.screen"),
        )
        assert result == "tui/**"

    # ── #2896: relocated slices — mocks may live under an entrypoint's own
    # sibling dir, not the shared repo-root tree this function used to
    # assume unconditionally.

    @staticmethod
    def _routed_config_with_entrypoint() -> Config:
        return Config(
            repos=[Repo(name="claude-coordinator", github="john/claude-coordinator")],
            machines=[],
            acceptance=AcceptanceConfig(
                drivers={
                    "claude-coordinator": AcceptanceDriverConfig(
                        routes=[
                            AcceptanceDriverConfig(
                                match="coord/**", kind="cli-pytest", run="pytest",
                            ),
                            AcceptanceDriverConfig(
                                match="tui/**", kind="tui-tuidriver", run="cargo test",
                                entrypoint="tui/tests/acceptance.rs",
                            ),
                        ]
                    )
                }
            ),
        )

    def test_searches_every_root_and_finds_a_relocated_slices_mocks(self) -> None:
        """ms-65's mocks now live under `tui/tests/acceptance/ms-65/mocks/`
        (relocated out of the shared repo-root tree, #2896) — the lister
        returns nothing at the legacy repo-root path and the relocated
        `.screen` files at the sibling one; resolution must still succeed."""
        cfg = self._routed_config_with_entrypoint()

        def lister(repo_github: str, path: str, branch: str) -> tuple[str, ...]:
            if path == "tui/tests/acceptance/ms-65/mocks":
                return ("board-tabs.screen",)
            return ()

        result = resolve_for_path(cfg, cfg.repo("claude-coordinator"), 65, list_mock_dir=lister)
        assert result == "tui/**"

    def test_queries_both_the_shared_tree_and_the_entrypoint_sibling_dir(self) -> None:
        cfg = self._routed_config_with_entrypoint()
        calls: list[tuple] = []
        # Neither candidate has any mocks in this test — resolution can't
        # succeed, but both roots must still have been queried before it
        # gives up (the thing under test here).
        with pytest.raises(ForPathResolutionError):
            resolve_for_path(
                cfg, cfg.repo("claude-coordinator"), 65,
                list_mock_dir=lambda *a: calls.append(a) or (),
            )
        assert calls == [
            ("john/claude-coordinator", "tests/acceptance/ms-65/mocks", "main"),
            ("john/claude-coordinator", "tui/tests/acceptance/ms-65/mocks", "main"),
        ]

    def test_no_recognized_mocks_raises(self) -> None:
        cfg = self._routed_config()
        with pytest.raises(ForPathResolutionError, match="no recognized mock files"):
            resolve_for_path(
                cfg, cfg.repo("claude-coordinator"), 38,
                list_mock_dir=lambda *a: (),
            )

    def test_mixed_mock_kinds_raises(self) -> None:
        cfg = self._routed_config()
        with pytest.raises(ForPathResolutionError, match="mixed mock kinds"):
            resolve_for_path(
                cfg, cfg.repo("claude-coordinator"), 38,
                list_mock_dir=lambda *a: ("a.screen", "b.out"),
            )

    def test_kind_with_no_matching_route_raises(self) -> None:
        cfg = Config(
            repos=[Repo(name="claude-coordinator", github="john/claude-coordinator")],
            machines=[],
            acceptance=AcceptanceConfig(
                drivers={
                    "claude-coordinator": AcceptanceDriverConfig(
                        routes=[
                            AcceptanceDriverConfig(
                                match="coord/**", kind="cli-pytest", run="pytest",
                            ),
                        ]
                    )
                }
            ),
        )
        with pytest.raises(ForPathResolutionError, match="matches 0 routes"):
            resolve_for_path(
                cfg, cfg.repo("claude-coordinator"), 38,
                list_mock_dir=lambda *a: ("a.screen",),
            )

    def test_error_message_names_the_no_acceptance_and_manual_for_path_escape_hatches(
        self,
    ) -> None:
        cfg = self._routed_config()
        with pytest.raises(ForPathResolutionError) as exc:
            resolve_for_path(
                cfg, cfg.repo("claude-coordinator"), 38,
                list_mock_dir=lambda *a: (),
            )
        assert "--no-acceptance" in str(exc.value)
        assert "--for-path" in str(exc.value)
