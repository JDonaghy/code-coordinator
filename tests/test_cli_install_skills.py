"""Tests for `coord install-skills` (#319)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from click.testing import CliRunner

from coord.cli import main

from .conftest import output_and_stderr


# ── Helpers ────────────────────────────────────────────────────────────────


def _fake_skills_ref(skills: dict[str, str]) -> MagicMock:
    """Build a mock importlib.resources Traversable for the bundled skills dir.

    ``skills`` maps skill name → SKILL.md content.  Every entry gets a
    SKILL.md that is readable; use an empty dict to simulate zero skills.
    """
    entries: list[MagicMock] = []
    for name, content in skills.items():
        entry = MagicMock()
        entry.name = name
        skill_md = MagicMock()
        skill_md.read_text.return_value = content
        entry.joinpath.return_value = skill_md
        entries.append(entry)

    ref = MagicMock()
    ref.iterdir.return_value = iter(entries)
    return ref


def _invoke(
    args: list[str],
    skills: dict[str, str],
    home_dir: Path,
) -> object:
    """Invoke ``coord install-skills`` with fake bundled skills and home dir."""
    fake_ref = _fake_skills_ref(skills)

    with (
        patch("importlib.resources.files") as mock_files,
        patch.object(Path, "home", return_value=home_dir),
    ):
        mock_files.return_value.joinpath.return_value = fake_ref
        return CliRunner().invoke(main, ["install-skills", *args])


# ── --list ─────────────────────────────────────────────────────────────────


class TestInstallSkillsList:
    def test_list_no_skills_installed(self, tmp_path: Path) -> None:
        """--list shows 'not installed' when ~/.claude/skills/ is empty."""
        home = tmp_path / "home"
        home.mkdir()
        # skills dir does not exist under home — nothing pre-installed
        result = _invoke(["--list"], {"update-issue": "# skill"}, home)

        assert result.exit_code == 0, output_and_stderr(result)
        out = output_and_stderr(result)
        assert "update-issue" in out
        assert "not installed" in out

    def test_list_shows_installed_when_skill_file_present(self, tmp_path: Path) -> None:
        """--list shows 'installed' when SKILL.md already exists on disk."""
        home = tmp_path / "home"
        # Pre-install the skill file so the status check finds it.
        skill_path = home / ".claude" / "skills" / "update-issue"
        skill_path.mkdir(parents=True)
        (skill_path / "SKILL.md").write_text("# existing skill", encoding="utf-8")

        result = _invoke(["--list"], {"update-issue": "# skill"}, home)

        assert result.exit_code == 0, output_and_stderr(result)
        out = output_and_stderr(result)
        assert "update-issue" in out
        assert "installed" in out
        assert "not installed" not in out

    def test_list_multiple_skills(self, tmp_path: Path) -> None:
        """--list shows all bundled skills."""
        home = tmp_path / "home"
        home.mkdir()
        result = _invoke(
            ["--list"],
            {"skill-a": "# a", "skill-b": "# b"},
            home,
        )

        assert result.exit_code == 0, output_and_stderr(result)
        out = output_and_stderr(result)
        assert "skill-a" in out
        assert "skill-b" in out

    def test_list_writes_nothing_to_disk(self, tmp_path: Path) -> None:
        """--list must not create any files."""
        home = tmp_path / "home"
        home.mkdir()
        skills_root = home / ".claude" / "skills"

        _invoke(["--list"], {"update-issue": "# skill"}, home)

        assert not skills_root.exists(), "--list must not create the skills directory"

    def test_list_with_dry_run_warns(self, tmp_path: Path) -> None:
        """--list --dry-run should emit a warning (dry-run is a no-op with list)."""
        home = tmp_path / "home"
        home.mkdir()
        result = _invoke(["--list", "--dry-run"], {"update-issue": "# skill"}, home)

        assert result.exit_code == 0, output_and_stderr(result)
        out = output_and_stderr(result)
        assert "warning" in out.lower() or "no effect" in out.lower()


# ── --dry-run ──────────────────────────────────────────────────────────────


class TestInstallSkillsDryRun:
    def test_dry_run_prints_would_install(self, tmp_path: Path) -> None:
        """--dry-run reports 'would install' for a skill not yet on disk."""
        home = tmp_path / "home"
        home.mkdir()
        result = _invoke(["--dry-run"], {"update-issue": "# skill"}, home)

        assert result.exit_code == 0, output_and_stderr(result)
        out = output_and_stderr(result)
        assert "would install" in out

    def test_dry_run_prints_would_update(self, tmp_path: Path) -> None:
        """--dry-run reports 'would update' for a skill that already exists."""
        home = tmp_path / "home"
        skill_path = home / ".claude" / "skills" / "update-issue"
        skill_path.mkdir(parents=True)
        (skill_path / "SKILL.md").write_text("# old", encoding="utf-8")

        result = _invoke(["--dry-run"], {"update-issue": "# new"}, home)

        assert result.exit_code == 0, output_and_stderr(result)
        assert "would update" in output_and_stderr(result)

    def test_dry_run_writes_no_files(self, tmp_path: Path) -> None:
        """--dry-run must not write any files to disk."""
        home = tmp_path / "home"
        home.mkdir()
        skills_root = home / ".claude" / "skills"

        _invoke(["--dry-run"], {"update-issue": "# skill"}, home)

        assert not skills_root.exists(), "--dry-run must not create the skills directory"


# ── Happy-path install ──────────────────────────────────────────────────────


class TestInstallSkillsInstall:
    def test_install_writes_skill_md(self, tmp_path: Path) -> None:
        """install-skills writes SKILL.md to ~/.claude/skills/<name>/SKILL.md."""
        home = tmp_path / "home"
        home.mkdir()
        skill_content = "# /update-issue\nsome content"

        result = _invoke([], {"update-issue": skill_content}, home)

        assert result.exit_code == 0, output_and_stderr(result)
        dest = home / ".claude" / "skills" / "update-issue" / "SKILL.md"
        assert dest.exists(), "SKILL.md was not written"
        assert dest.read_text(encoding="utf-8") == skill_content

    def test_install_reports_installed_action(self, tmp_path: Path) -> None:
        """Output should include 'installed' for a new skill."""
        home = tmp_path / "home"
        home.mkdir()
        result = _invoke([], {"update-issue": "# skill"}, home)

        assert result.exit_code == 0, output_and_stderr(result)
        assert "installed" in output_and_stderr(result)

    def test_install_creates_parent_directories(self, tmp_path: Path) -> None:
        """~/.claude/skills/ and skill subdirectory are created automatically."""
        home = tmp_path / "home"
        home.mkdir()
        # Make sure neither directory exists before the install.
        assert not (home / ".claude").exists()

        _invoke([], {"update-issue": "# skill"}, home)

        assert (home / ".claude" / "skills" / "update-issue").is_dir()

    def test_install_done_message_lists_skills(self, tmp_path: Path) -> None:
        """Completion message should mention the installed skill names."""
        home = tmp_path / "home"
        home.mkdir()
        result = _invoke([], {"my-skill": "# skill"}, home)

        assert result.exit_code == 0, output_and_stderr(result)
        out = output_and_stderr(result)
        # Dynamic message should reference the skill, not a hardcoded name.
        assert "my-skill" in out or "/my-skill" in out


# ── Happy-path update ───────────────────────────────────────────────────────


class TestInstallSkillsUpdate:
    def test_update_overwrites_existing_skill(self, tmp_path: Path) -> None:
        """Re-installing a skill that already exists updates the file content."""
        home = tmp_path / "home"
        dest = home / ".claude" / "skills" / "update-issue"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("# old content", encoding="utf-8")

        new_content = "# new content"
        result = _invoke([], {"update-issue": new_content}, home)

        assert result.exit_code == 0, output_and_stderr(result)
        assert (dest / "SKILL.md").read_text(encoding="utf-8") == new_content

    def test_update_reports_updated_action(self, tmp_path: Path) -> None:
        """Output should include 'updated' when SKILL.md already existed."""
        home = tmp_path / "home"
        dest = home / ".claude" / "skills" / "update-issue"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("# old", encoding="utf-8")

        result = _invoke([], {"update-issue": "# new"}, home)

        assert result.exit_code == 0, output_and_stderr(result)
        assert "updated" in output_and_stderr(result)


# ── Error paths ─────────────────────────────────────────────────────────────


class TestInstallSkillsErrors:
    def test_error_when_skills_dir_not_readable(self, tmp_path: Path) -> None:
        """When skills_ref.iterdir() raises, the command exits with an error."""
        home = tmp_path / "home"
        home.mkdir()

        unreadable_ref = MagicMock()
        unreadable_ref.iterdir.side_effect = FileNotFoundError("no such dir")

        with (
            patch("importlib.resources.files") as mock_files,
            patch.object(Path, "home", return_value=home),
        ):
            mock_files.return_value.joinpath.return_value = unreadable_ref
            result = CliRunner().invoke(main, ["install-skills"])

        assert result.exit_code != 0
        out = output_and_stderr(result)
        assert "error" in out.lower()

    def test_no_bundled_skills_exits_cleanly(self, tmp_path: Path) -> None:
        """If the package has zero skills, exit 0 with an informative message."""
        home = tmp_path / "home"
        home.mkdir()
        result = _invoke([], {}, home)  # empty dict → no skills

        assert result.exit_code == 0, output_and_stderr(result)
        assert "no bundled skills" in output_and_stderr(result).lower()

    def test_error_when_package_not_found(self, tmp_path: Path) -> None:
        """If importlib.resources.files raises, the command exits with an error."""
        home = tmp_path / "home"
        home.mkdir()

        with (
            patch("importlib.resources.files", side_effect=ModuleNotFoundError("coord")),
            patch.object(Path, "home", return_value=home),
        ):
            result = CliRunner().invoke(main, ["install-skills"])

        assert result.exit_code != 0
        out = output_and_stderr(result)
        assert "error" in out.lower()


class TestBundledSkillFrontmatter:
    """Every bundled `coord/skills/*/SKILL.md`'s YAML frontmatter must parse.

    A broken frontmatter block (e.g. an unquoted `trigger:` value containing
    a bare colon) silently breaks that skill's discoverability/invocation as
    a `/slash-command` in the harness that parses it, without failing
    `coord install-skills` itself — the copy/sync mechanism doesn't care
    whether the frontmatter is valid YAML. Catch it here instead (#2866).
    """

    def _bundled_skill_md_paths(self) -> list[Path]:
        skills_root = Path(__file__).resolve().parent.parent / "coord" / "skills"
        paths = sorted(skills_root.glob("*/SKILL.md"))
        assert paths, f"expected at least one bundled skill under {skills_root}"
        return paths

    def test_frontmatter_parses_as_yaml(self) -> None:
        for skill_md in self._bundled_skill_md_paths():
            text = skill_md.read_text(encoding="utf-8")
            assert text.startswith("---\n"), (
                f"{skill_md} does not start with a YAML frontmatter block"
            )
            end = text.find("\n---", 3)
            assert end != -1, f"{skill_md} has an unterminated frontmatter block"
            frontmatter = text[3:end]
            try:
                parsed = yaml.safe_load(frontmatter)
            except yaml.YAMLError as exc:
                raise AssertionError(f"{skill_md} frontmatter is not valid YAML: {exc}") from exc
            assert isinstance(parsed, dict), f"{skill_md} frontmatter must parse to a mapping"
