"""#2900: the Python↔Rust mirror inventory cannot silently drift.

`docs/ADR_PY_RUST_MIRRORS.md` enumerates every place `coord/**.py` re-derives
a rule that `coord-tui` also re-derives independently. Since #2899 those two
halves live in different repositories, so the only thing that used to keep
them in step — both files being one `grep` apart in one checkout — is gone.

The ADR's whole claim is that the list is **complete**. A document making that
claim without a test is a document that is true on the day it is written. So:
every reference in `coord/**.py` to a coord-tui source file must appear in the
ADR. Adding a mirror then costs one table row, which is the point — it makes
adding one a visible decision rather than an incidental comment.

This does NOT verify that the mirrored *logic* still agrees. Nothing here
does, deliberately; see the ADR's "Decision" section for why a wire-level fix,
not a second mirror, is the remedy when one bites.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COORD_PKG = REPO_ROOT / "coord"
ADR = REPO_ROOT / "docs" / "ADR_PY_RUST_MIRRORS.md"

# The pre-#2899 in-repo spelling, which is what every existing comment uses.
# See the ADR's "Consequences" section for why those were not rewritten.
_MIRROR_RE = re.compile(r"tui/src/[A-Za-z0-9_./-]*\.rs")


def _python_sources() -> list[Path]:
    return sorted(p for p in COORD_PKG.rglob("*.py") if "__pycache__" not in p.parts)


def mirror_references() -> dict[str, set[str]]:
    """``{"coord/stage_projection.py": {"tui/src/app/pipeline.rs"}}``."""
    found: dict[str, set[str]] = {}
    for path in _python_sources():
        hits = set(_MIRROR_RE.findall(path.read_text(encoding="utf-8", errors="replace")))
        if hits:
            found[str(path.relative_to(REPO_ROOT))] = hits
    return found


def test_the_adr_exists_and_names_its_issue() -> None:
    assert ADR.is_file()
    text = ADR.read_text(encoding="utf-8")
    assert "#2900" in text
    assert "#2899" in text


def test_every_mirrored_python_module_is_listed_in_the_adr() -> None:
    """A mirror the ADR does not know about is a mirror nobody is tracking."""
    adr = ADR.read_text(encoding="utf-8")
    missing = sorted(mod for mod in mirror_references() if mod not in adr)
    assert not missing, (
        f"{', '.join(missing)} re-derive(s) coord-tui logic but is not in "
        f"docs/ADR_PY_RUST_MIRRORS.md. Add a table row — see that document's "
        "'Decision' section for why the list has to stay complete."
    )


def test_every_mirrored_rust_file_is_named_in_the_adr() -> None:
    """The Rust half has to be nameable too, or the row is not actionable."""
    adr = ADR.read_text(encoding="utf-8")
    all_rust = {ref for refs in mirror_references().values() for ref in refs}
    # The ADR spells these post-split (`src/app/pipeline.rs`), so compare on
    # the part that survives the move.
    missing = sorted(ref for ref in all_rust if ref[len("tui/") :] not in adr)
    assert not missing, (
        f"docs/ADR_PY_RUST_MIRRORS.md names no coord-tui file for {missing}."
    )


def test_the_detector_actually_detects_something() -> None:
    """A guard whose subject has silently become empty passes forever.

    If this ever legitimately drops to zero — every mirror closed — delete
    this file and the ADR together, on purpose, rather than leaving a green
    test measuring nothing.
    """
    refs = mirror_references()
    assert len(refs) >= 10, (
        f"only {len(refs)} mirrored modules found — did the reference spelling "
        "change? _MIRROR_RE matches the pre-#2899 `tui/src/...` form."
    )
    assert "coord/stage_projection.py" in refs


def test_the_wire_half_is_recorded_as_closed_not_pending() -> None:
    """#2900's own deliverable: the generated files are named as guarded, so a
    reader cannot mistake the wire for one of the unguarded mirrors."""
    adr = ADR.read_text(encoding="utf-8")
    assert "src/app/types/generated.rs" in adr
    assert "src/app/types/generated_requests.rs" in adr
    assert "coord_tui_ci_pin" in adr
