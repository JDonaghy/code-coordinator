"""One shared provisioning core, and the tests that stop it becoming two (#3139).

``scripts/lib/provision-core.sh`` owns *what a fleet machine needs*; the two
lanes that source it —

    scripts/azure-workers/provision-worker.sh   the Azure golden IMAGE lane
    scripts/provision-machine.sh                the bare-metal lane (#3138)

— own only the constraints of their substrate (root vs sudo, a dedicated
``coord`` user vs the operator's own account, zero identity vs interactive
identity).

Why this module exists at all, in the words of the issue: every value the core
now holds *"has already drifted once and cost something."* The opencode pin
exists because a version skew between image and fleet was a real failure
(#1777). The ``gh`` floor exists because Ubuntu's packaged ``gh`` is below it
and produces an image that fails the CI merge gate with
``GhTooOldForJsonChecks``. The ``/opt/rust`` location exists because a per-user
rustup install left ``cargo`` invisible to every dispatched task (#1671). A
second copy of any of them is a second chance for all three to come back, *in
the half nobody was looking at* — and the failure mode is asymmetric and quiet:
a drifted image is discovered ~30 minutes after a build reports success, at
deploy time, on a VM you are paying for.

So the tests below are of three kinds:

1. **Drift guards.** Each pinned value appears in exactly ONE place across both
   lanes, and the shell-side ``gh`` floor is checked against the real Python
   constant ``coord.github_ops.GH_PR_CHECKS_JSON_MIN_VERSION`` — the same
   spirit as ``tests/test_packaged_deploy_units.py``, which exists precisely
   because a byte-identical copy cannot be trusted to stay that way.

2. **Behaviour of the core itself.** Its predicates and helpers are sourced and
   *run*, with both verdicts exercised. A floor check that has only ever been
   observed passing is not a proven gate (#2096): every one below is driven to
   its failing branch too.

3. **The gotchas from ``docs/EPHEMERAL_WORKERS.md`` that are expressible as a
   check.** The NVMe ``DiskControllerTypes`` declaration and the ``gh`` floor
   both HARD-FAIL the image build today, and this module drives each guard for
   real (a stub ``az`` on ``$PATH`` for the first) so a future refactor cannot
   quietly weaken either into a warning.

What is deliberately NOT here: a real image build. That is the acceptance bar
the issue states in as many words, it costs an Azure subscription and ~30
minutes, and it is in the PR's SMOKE_TESTS block.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from coord.github_ops import GH_PR_CHECKS_JSON_MIN_VERSION
from coord.prereqs import BASELINE_PREREQS

from .conftest import POSIX_BASH

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE = REPO_ROOT / "scripts" / "lib" / "provision-core.sh"
IMAGE_LANE = REPO_ROOT / "scripts" / "azure-workers" / "provision-worker.sh"
METAL_LANE = REPO_ROOT / "scripts" / "provision-machine.sh"
BUILD_IMAGE = REPO_ROOT / "scripts" / "azure-workers" / "build-worker-image.sh"

#: Every script that provisions a fleet machine, or drives something that does.
#: A new lane added here without going through the core fails the drift guards
#: below, which is the point.
LANES = (IMAGE_LANE, METAL_LANE)


def _run(script: str, *, env_extra: dict[str, str] | None = None, cwd: Path | None = None):
    """Run a bash snippet, returning the CompletedProcess."""
    import os

    env = dict(os.environ)
    env.setdefault("COORD_CORE_SUDO", "")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [POSIX_BASH, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
        cwd=str(cwd) if cwd else None,
    )


def _with_core(body: str) -> str:
    return f'set -uo pipefail\n. {shlex.quote(str(CORE))}\n{body}\n'


def _core_value(name: str) -> str:
    """Read one variable out of the core by SOURCING it, as its callers do."""
    result = _run(_with_core(f'printf "%s" "${name}"'))
    assert result.returncode == 0, result.stderr
    return result.stdout


def _strip_comments(text: str) -> str:
    """Drop whole-line and trailing ``#`` comments.

    The drift guards below grep for *code* that restates a pinned value. A
    comment that mentions the value (``# the same floor provision-worker.sh
    enforces``) is exactly what the issue calls "the weakest possible link" —
    harmless prose, not a second copy — so it must not trip the guard, while
    an assignment must.
    """
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Trailing comment: only outside quotes. These scripts never embed a
        # literal ' #' inside a quoted string on a line that also assigns a
        # pinned value, so the cheap split is sufficient and stays readable.
        if " #" in line:
            line = line.split(" #", 1)[0]
        out.append(line)
    return "\n".join(out)


# ── 1. Drift guards: exactly one copy, fleet-wide ────────────────────────────


@pytest.mark.parametrize(
    ("core_var", "pattern", "what"),
    [
        (
            "COORD_GH_MIN_VERSION",
            r"\b2\.86\.0\b",
            "the gh version floor (coord.github_ops.GH_PR_CHECKS_JSON_MIN_VERSION)",
        ),
        (
            "COORD_OPENCODE_VERSION",
            r"\b1\.18\.11\b",
            "the opencode pin (#1777)",
        ),
        (
            "COORD_RUST_HOME",
            r"/opt/rust",
            "the system-wide rust location (#1671)",
        ),
        (
            "COORD_NODE_MAJOR",
            r"(?:NODE_MAJOR=|setup_)(?:\")?22(?:\")?",
            "the node major",
        ),
    ],
)
def test_each_pinned_value_appears_in_exactly_one_place_across_both_lanes(
    core_var: str, pattern: str, what: str
) -> None:
    """#3139 acceptance: a second literal anywhere in either lane fails here.

    This is the whole issue in one assertion. Before #3139 both lanes carried
    their own copy of the gh floor and the image lane alone carried the node
    major, the opencode pin and the rust location — each one a value that has
    already drifted once and cost something. After it, the literal exists in
    ``scripts/lib/provision-core.sh`` and nowhere else, and re-typing it into
    a lane is a test failure rather than a discovery made 30 minutes after a
    build reports success.
    """
    # It really is in the core, and the core really is the thing that defines
    # the variable both lanes use.
    core_code = _strip_comments(CORE.read_text(encoding="utf-8"))
    assert re.search(pattern, core_code), (
        f"{what} is not a literal in {CORE.relative_to(REPO_ROOT)} — "
        "the core is supposed to be the one place it lives"
    )
    assert _core_value(core_var), f"{core_var} is empty when the core is sourced"

    for lane in LANES:
        code = _strip_comments(lane.read_text(encoding="utf-8"))
        hits = re.findall(pattern, code)
        assert not hits, (
            f"{lane.relative_to(REPO_ROOT)} restates {what} ({hits!r}).\n"
            f"It must read ${core_var} from scripts/lib/provision-core.sh instead. "
            "Two copies that agree today are a split-brain waiting to happen, and "
            "the half nobody is looking at is the one that drifts."
        )


def test_the_shell_gh_floor_and_the_python_constant_cannot_disagree() -> None:
    """#3139 acceptance: the shell floor IS ``GH_PR_CHECKS_JSON_MIN_VERSION``.

    ``provision-worker.sh`` used to carry ``GH_MIN_VERSION="2.86.0"`` with the
    comment *"coord.github_ops.GH_PR_CHECKS_JSON_MIN_VERSION"*. A comment is
    the weakest possible link between two values that must agree; this test is
    a real one. Bump the Python constant without bumping the core and this
    fails — before an image is baked with a ``gh`` that cannot read the CI
    merge gate's JSON (``GhTooOldForJsonChecks``).
    """
    shell_floor = _core_value("COORD_GH_MIN_VERSION")
    assert shell_floor == GH_PR_CHECKS_JSON_MIN_VERSION, (
        f"scripts/lib/provision-core.sh pins COORD_GH_MIN_VERSION={shell_floor!r} "
        f"but coord.github_ops.GH_PR_CHECKS_JSON_MIN_VERSION is "
        f"{GH_PR_CHECKS_JSON_MIN_VERSION!r}. Both halves of the fleet install gh "
        "against the shell value; the merge gate checks against the Python one."
    )


def test_both_lanes_actually_source_the_core() -> None:
    """A lane that stopped sourcing the core would pass every drift guard above
    by simply not mentioning the values — and then install nothing. Assert the
    sourcing itself, and that a missing core is FATAL rather than a fallback to
    inlined defaults (an image built from half a toolchain list is exactly the
    silent drift this issue is paying off).
    """
    for lane in LANES:
        text = lane.read_text(encoding="utf-8")
        assert "provision-core.sh" in text, (
            f"{lane.relative_to(REPO_ROOT)} does not reference the shared core"
        )
        code = _strip_comments(text)
        assert re.search(r"^\s*\.\s+\"\$", code, re.MULTILINE), (
            f"{lane.relative_to(REPO_ROOT)} never sources anything"
        )
        assert "exit 1" in code, (
            f"{lane.relative_to(REPO_ROOT)} must hard-fail when the core is absent"
        )
        # "One question, one answer": the gh floor, the Python floor and the
        # base package list are each asked through the core's function, not
        # re-derived. Two implementations that agree today are a split-brain
        # waiting to happen.
        for fn in ("coord_core_gh_meets_floor", "coord_core_python_meets_floor"):
            assert fn in code, (
                f"{lane.relative_to(REPO_ROOT)} does not ask the core's {fn}() — "
                "it has grown its own second answer to a question the core owns"
            )
        assert "sort -V" not in code, (
            f"{lane.relative_to(REPO_ROOT)} has its own version comparison again; "
            "coord_core_version_meets_floor is the one implementation"
        )


def test_a_lane_that_cannot_find_the_core_refuses_to_run() -> None:
    """The failing branch of the above, driven for real.

    ``build-worker-image.sh`` scp's the image lane to a throwaway builder. If
    the core did not come along, the script must exit non-zero rather than
    provision an image from whatever defaults happen to be lying around.
    """
    result = _run(
        f"COORD_PROVISION_CORE=/nonexistent/provision-core.sh "
        f"{shlex.quote(POSIX_BASH)} {shlex.quote(str(IMAGE_LANE))} 2>&1 || echo RC=$?"
    )
    assert "RC=" in result.stdout, f"the image lane did not fail: {result.stdout}"
    assert "provision-core.sh" in result.stdout


def test_the_builder_receives_the_core_alongside_the_image_lane() -> None:
    """#3139: the image lane now has a dependency that must reach the builder VM.

    ``build-worker-image.sh`` scp's ``provision-worker.sh`` to ``/tmp`` on a
    throwaway builder. Sourcing a file that was never copied would fail the
    build at step 2/6 — which is loud and cheap — but only if the copy is
    actually there. Assert it is, and that the script confirms the file landed
    rather than trusting scp's exit code (#2096).
    """
    text = BUILD_IMAGE.read_text(encoding="utf-8")
    assert "provision-core.sh" in text, (
        "build-worker-image.sh does not copy the shared core to the builder"
    )
    assert "test -f /tmp/lib/provision-core.sh" in text, (
        "build-worker-image.sh does not confirm the core landed on the builder — "
        "scp exiting 0 is not evidence the file is there"
    )


def test_the_repo_clone_list_is_shared() -> None:
    """The list of repos a fleet machine clones lived in both lanes, in two
    different shapes (a bash array and a comma-separated string). One value now,
    rendered two ways by the core."""
    repos = _core_value("COORD_FLEET_REPOS").split()
    assert repos, "the core defines no fleet repo list"
    csv = _run(_with_core("coord_core_repos_csv")).stdout.strip()
    assert csv == ",".join(repos)
    for lane in LANES:
        code = _strip_comments(lane.read_text(encoding="utf-8"))
        for restatement in (csv, " ".join(repos)):
            assert restatement not in code, (
                f"{lane.relative_to(REPO_ROOT)} restates the repo clone list "
                f"({restatement!r}) instead of reading $COORD_FLEET_REPOS"
            )


# ── 2. The core's own behaviour, both verdicts ───────────────────────────────


@pytest.mark.parametrize(
    ("version", "expected_ok"),
    [
        ("2.86.0", True),   # exactly the floor
        ("2.90.1", True),
        ("10.0.0", True),   # sort -V, not lexicographic
        ("2.85.9", False),
        ("2.9.0", False),   # 2.9 < 2.86 under version ordering
        ("", False),        # a probe that produced nothing is NOT a pass
        ("unknown", False),
    ],
)
def test_the_version_floor_comparison_can_fail(version: str, expected_ok: bool) -> None:
    """#2096: a gate must be able to fail, and an ABSENT measurement must not
    default to the permissive branch. Both lanes previously had their own
    ``sort -V`` incantation; this is now the one implementation, so the two
    cannot disagree about what "meets the floor" means."""
    floor = GH_PR_CHECKS_JSON_MIN_VERSION
    result = _run(
        _with_core(
            f"if coord_core_version_meets_floor {shlex.quote(floor)} "
            f"{shlex.quote(version)}; then echo OK; else echo BELOW; fi"
        )
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("OK" if expected_ok else "BELOW")


@pytest.mark.parametrize(
    ("stub_output", "expected"),
    [
        ("gh version 2.90.0 (2026-01-01)", "OK"),
        ("gh version 2.86.0 (2026-01-01)", "OK"),
        ("gh version 2.40.1 (2024-01-01)", "BELOW"),  # Ubuntu's own gh, roughly
        ("", "BELOW"),                                # gh that says nothing
    ],
)
def test_the_gh_floor_hard_fails_on_an_old_or_silent_gh(
    tmp_path: Path, stub_output: str, expected: str
) -> None:
    """docs/EPHEMERAL_WORKERS.md gotcha, as a named test: *"apt install gh
    produces a broken image."*

    Ubuntu's ``gh`` is far below ``GH_PR_CHECKS_JSON_MIN_VERSION`` and the CI
    merge gate throws ``GhTooOldForJsonChecks`` below it. The image build must
    HARD-FAIL there, not warn — otherwise the image bakes, publishes, deploys,
    and the failure surfaces at a merge gate hours later. Driven against a stub
    ``gh`` so the failing verdict is genuinely reachable, not just asserted
    about in prose.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(stub_output)}\n")
    gh.chmod(0o755)

    result = _run(
        _with_core("if coord_core_gh_meets_floor; then echo OK; else echo BELOW; fi"),
        env_extra={"PATH": f"{bindir}:{_path()}"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_a_missing_gh_is_below_the_floor_not_above_it(tmp_path: Path) -> None:
    """The permissive-default shape (#2096) in its purest form: no ``gh`` on
    ``PATH`` at all must read BELOW, never "no version to compare, carry on"."""
    empty = tmp_path / "emptybin"
    empty.mkdir()
    result = _run(
        _with_core("if coord_core_gh_meets_floor; then echo OK; else echo BELOW; fi"),
        # Genuinely empty: this host has a real gh, and a PATH that still found
        # it would make the test assert nothing.
        env_extra={"PATH": str(empty)},
    )
    assert result.stdout.strip() == "BELOW", result.stdout


@pytest.mark.parametrize(
    ("version_line", "expected"),
    [
        ("Python 3.12.3", "OK"),
        ("Python 3.13.0", "OK"),
        ("Python 3.20.0", "OK"),
        ("Python 3.11.9", "BELOW"),
        ("Python 3.9.18", "BELOW"),
    ],
)
def test_the_python_floor_can_fail(tmp_path: Path, version_line: str, expected: str) -> None:
    """install-agent.sh refuses below 3.12 and both lanes used to spell the same
    regex twice. One spelling now, and it is driven to both verdicts."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    py = bindir / "python3"
    py.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(version_line)}\n")
    py.chmod(0o755)
    result = _run(
        _with_core("if coord_core_python_meets_floor; then echo OK; else echo BELOW; fi"),
        env_extra={"PATH": f"{bindir}:/usr/bin:/bin"},
    )
    assert result.stdout.strip() == expected, result.stdout


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("1.18.11", "MATCH"),
        ("1.18.12", "SKEW"),   # newer is still a skew — #1777 was either direction
        ("1.18.1", "SKEW"),
        ("", "SKEW"),
    ],
)
def test_the_opencode_pin_is_exact_in_both_directions(reported: str, expected: str) -> None:
    """#1777: the failure was a SKEW between the image and the standing fleet,
    so the check is equality, not a floor. A NEWER opencode on the image is a
    failure too, and an empty answer is never a pass."""
    result = _run(
        _with_core(
            f"if coord_core_opencode_version_matches {shlex.quote(reported)}; "
            "then echo MATCH; else echo SKEW; fi"
        )
    )
    assert result.stdout.strip() == expected, result.stdout


def test_the_opencode_install_command_carries_the_pin_and_no_login() -> None:
    """A golden image must contain zero identity: the install command must pin
    the version and must never authenticate (no ``auth login``, no auth.json).
    The credential arrives per-boot from Key Vault."""
    pin = _core_value("COORD_OPENCODE_VERSION")
    cmd = _run(_with_core("coord_core_opencode_install_cmd")).stdout
    assert f"--version {pin}" in cmd, cmd
    assert "--no-modify-path" in cmd, cmd
    assert "auth" not in cmd, f"the image lane must not authenticate opencode: {cmd}"

    link = _run(_with_core("coord_core_opencode_link_cmd")).stdout
    # The ~/.opencode/bin PATH problem: the installer's destination is
    # unconditional, so the fix is a symlink into ~/.local/bin, which the
    # coord-agent unit's PATH already includes.
    assert ".opencode/bin/opencode" in link and ".local/bin/opencode" in link, link


def test_the_base_package_list_is_shared_and_deduplicated() -> None:
    """The image lane installed a flat apt list; the bare-metal lane probed a
    ``probe|package`` table. One table now, and the image lane derives its apt
    arguments from it — so a package this fleet needs cannot be added to one
    lane and forgotten in the other."""
    packages = _run(_with_core("coord_core_base_packages")).stdout.split()
    assert packages, "the core produced no base package list"
    assert len(packages) == len(set(packages)), f"duplicate apt packages: {packages}"
    # The tools every dispatched worker needs to exist at all. tmux in
    # particular: coord/interactive.py, drive.py, terminal and reattach all
    # shell out to it, so a worker without it fails at DISPATCH, not at build.
    for required in ("git", "curl", "jq", "tmux", "python3-venv", "ripgrep", "build-essential"):
        assert required in packages, f"{required} fell out of the shared base list"


def test_the_prereq_check_list_covers_every_python_baseline_prereq() -> None:
    """The image lane's step 9/9 says it *"mirrors coord/prereqs.py"*. Make that
    a real link too: a prereq added to ``BASELINE_PREREQS`` must not silently
    stop being verified at provisioning time, which is the only moment the
    problem is cheap to fix."""
    # One entry per line — the entries themselves contain spaces ("git|git
    # --version"), so a space-joined ${...[*]} would shred them.
    listing = _run(_with_core('printf "%s\\n" "${COORD_PREREQ_CHECKS[@]}"'))
    assert listing.returncode == 0, listing.stderr
    checked = {line.split("|", 1)[0] for line in listing.stdout.splitlines() if "|" in line}
    assert checked, "the core defines no prereq checks"
    for prereq in BASELINE_PREREQS:
        assert prereq.tool in checked, (
            f"coord.prereqs.BASELINE_PREREQS names {prereq.tool!r} but the shared "
            "provisioning core never verifies it. What breaks: "
            f"{prereq.what_breaks}"
        )


def test_the_prereq_verification_reports_a_missing_tool_as_a_failure(tmp_path: Path) -> None:
    """#2096: a verification pass whose failing verdict is unreachable is not a
    verification. With an empty ``PATH`` every check must report MISSING and the
    function must return non-zero, so ``provision-worker.sh`` refuses to
    generalize the image."""
    empty = tmp_path / "emptybin"
    empty.mkdir()
    result = _run(
        _with_core(
            "coord_core_verify_baseline_prereqs; echo RC=$?"
        ),
        env_extra={"PATH": str(empty)},
    )
    assert "MISSING" in result.stdout, result.stdout
    rc_line = [ln for ln in result.stdout.splitlines() if ln.startswith("RC=")]
    assert rc_line and rc_line[-1] != "RC=0", (
        f"a fleet machine with no tools at all was graded a pass: {result.stdout}"
    )


def test_sourcing_the_core_twice_is_safe() -> None:
    """Both lanes may source it more than once (a wrapper that re-execs, a
    verification script that also reads it). A second source must be a no-op,
    not an abort under ``set -e``."""
    result = _run(
        f"set -euo pipefail\n"
        f". {shlex.quote(str(CORE))}\n"
        f". {shlex.quote(str(CORE))}\n"
        f'printf "%s" "$COORD_GH_MIN_VERSION"'
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == GH_PR_CHECKS_JSON_MIN_VERSION


# ── 3. The EPHEMERAL_WORKERS.md gotchas that are expressible as a check ──────


@pytest.mark.parametrize(
    ("declared", "expect_fail"),
    [
        ("SCSI,NVMe", False),
        ("NVMe", False),
        ("SCSI", True),      # the pre-fix definition: boots nothing on v6/v7
        ("", True),          # `az` errored, or a definition with no features
    ],
)
def test_the_build_hard_fails_on_an_image_definition_without_nvme(
    tmp_path: Path, declared: str, expect_fail: bool
) -> None:
    """docs/EPHEMERAL_WORKERS.md gotcha, as a named test: *"Gallery image
    features are immutable."*

    ``DiskControllerTypes`` must be declared at definition-create time. Omit
    NVMe and you get a SCSI-only image that v6/v7 SKUs refuse to boot, with
    ``"cannot boot with OS image or disk"`` at DEPLOY time — half an hour after
    the build reported success. It cannot be added later; the only fix is
    deleting the definition and rebuilding.

    So the guard must be an ``exit 1``, never a warning, and its failing branch
    must be reachable. This drives ``require_nvme_declared`` for real against a
    stub ``az``: an old SCSI-only definition, and an ``az`` that answers with
    nothing at all, both fail.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    az = bindir / "az"
    az.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(declared)}\n")
    az.chmod(0o755)

    result = _run(
        # build-worker-image.sh's own `set -euo pipefail` runs when it is
        # sourced, so the guard's `return 1` would abort this harness before it
        # could record the verdict — which is itself the proof that the guard
        # HARD-FAILS the real build rather than warning. Relax errexit only to
        # capture the exit status.
        f"source {shlex.quote(str(BUILD_IMAGE))}\n"
        f"set +e\n"
        f"require_nvme_declared rg-coord-images sigcoord coord-worker; echo RC=$?",
        env_extra={"PATH": f"{bindir}:{_path()}"},
    )
    rc_lines = [ln for ln in result.stdout.splitlines() if ln.startswith("RC=")]
    assert rc_lines, f"require_nvme_declared did not run: {result.stdout}\n{result.stderr}"
    if expect_fail:
        assert rc_lines[-1] != "RC=0", (
            f"DiskControllerTypes={declared!r} was accepted. That image boots on "
            "nothing the fleet deploys, and you find out at deploy time."
        )
        assert "immutable" in result.stderr.lower(), result.stderr
        # ...and it is a HARD fail, not a warning: under the script's own
        # `set -euo pipefail` the guard aborts the run outright. If someone
        # ever softens `return 1` to a `warn`, this is what catches it.
        hard = _run(
            f"source {shlex.quote(str(BUILD_IMAGE))}\n"
            f"require_nvme_declared rg-coord-images sigcoord coord-worker\n"
            f"echo REACHED-THE-BUILD",
            env_extra={"PATH": f"{bindir}:{_path()}"},
        )
        assert hard.returncode != 0, hard.stdout
        assert "REACHED-THE-BUILD" not in hard.stdout, (
            "the NVMe guard warned instead of hard-failing — the build would "
            "publish an image that boots on nothing, and you find out at deploy "
            "time, half an hour after it reported success"
        )
    else:
        assert rc_lines[-1] == "RC=0", f"{result.stdout}\n{result.stderr}"


def test_the_nvme_declaration_is_still_made_at_definition_create_time() -> None:
    """The guard above only catches a definition someone else created. The build
    must also DECLARE the feature when it creates one — the two halves of the
    same gotcha."""
    text = BUILD_IMAGE.read_text(encoding="utf-8")
    assert "DiskControllerTypes=SCSI,NVMe" in text, (
        "build-worker-image.sh no longer declares NVMe at image-definition "
        "create time; features are immutable, so every image built from a "
        "definition it creates would be unbootable on v6/v7 SKUs"
    )


def test_the_image_lane_still_builds_as_a_dedicated_non_provisioning_user() -> None:
    """docs/EPHEMERAL_WORKERS.md gotcha: *"Do not build the image as the
    provisioning user."*

    ``waagent -deprovision+user`` deletes the provisioning user AND ITS HOME —
    which is where ``~/.coord-venv``, ``~/src`` and ``~/.npm`` live. Building as
    ``azureuser`` means the scrub silently throws the entire image away. The
    #3139 move must not have quietly relocated any of that onto the
    provisioning user.
    """
    code = _strip_comments(IMAGE_LANE.read_text(encoding="utf-8"))
    assert re.search(r'COORD_USER="\$\{COORD_USER:-coord\}"', code), (
        "the image lane no longer creates a dedicated coord user"
    )
    assert "useradd --create-home" in code
    # Everything expensive is still baked THROUGH that user, not as root.
    for expensive in ("~/.coord-venv", "~/src", "npm install -g"):
        assert expensive in code
    for line in code.splitlines():
        if any(marker in line for marker in ("~/.coord-venv", "git clone --filter")):
            assert "as_coord" in line, (
                f"this line bakes into a home directory without as_coord, so "
                f"`waagent -deprovision+user` will delete it: {line.strip()}"
            )


def test_the_image_lane_still_installs_zero_identity() -> None:
    """docs/EPHEMERAL_WORKERS.md, twice over: tailscale is installed but never
    brought ``up`` (node identity is minted per-boot as an ephemeral tagged
    key), and opencode is never authenticated. A golden image containing any
    identity gives every VM the same one."""
    code = _strip_comments(IMAGE_LANE.read_text(encoding="utf-8"))
    assert "tailscale.com/install.sh" in code
    assert not re.search(r"^\s*tailscale up", code, re.MULTILINE), (
        "the golden image must never run `tailscale up` — baking tailscaled.state "
        "would give every VM the same node identity"
    )
    assert "auth login" not in code, "the golden image must contain zero identity"


def test_the_bare_metal_lane_still_refuses_to_run_as_root() -> None:
    """The counterpart substrate rule, from the other lane's header: the
    operator IS the fleet user on bare metal, and creating a second account
    would put ``~/.coord-venv``, ``~/src`` and ``~/.claude`` somewhere the agent
    never looks. Sharing a core must not have leaked the image lane's root
    assumption into it."""
    result = _run(
        f"{shlex.quote(POSIX_BASH)} {shlex.quote(str(METAL_LANE))} --role worker --dry-run",
        env_extra={"COORD_PROVISION_ASSUME_YES": "1"},
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "PLAN:" in result.stdout
    code = _strip_comments(METAL_LANE.read_text(encoding="utf-8"))
    assert 'refusing to run as root' in code
    assert "useradd" not in code, "the bare-metal lane must never create a user"


def test_the_dry_run_plan_is_unchanged_by_the_extraction() -> None:
    """#3139 asks for *"no behaviour change to either lane beyond
    deduplication"*. The bare-metal lane's phase table is its externally
    visible contract (#3138 tests assert on the ordering invariant), so pin it
    here: the move must not have added, removed or reordered a phase."""
    result = _run(
        f"{shlex.quote(POSIX_BASH)} {shlex.quote(str(METAL_LANE))} --role server --dry-run"
    )
    assert result.returncode == 0, result.stderr
    phases = [
        line.split()[2]
        for line in result.stdout.splitlines()
        if line.startswith("PLAN:") and re.match(r"PLAN:\s+\d+\s", line)
    ]
    assert phases == [
        "preflight",
        "base-packages",
        "cred-tools",
        "coord-cli",
        "role-declaration",
        "credentials",
        "register",
        "toolchains",
        "repos",
        "daemon-units",
        "store",
        "gate",
    ], phases


def _path() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="these drive real POSIX shell scripts that provision Ubuntu hosts",
)
