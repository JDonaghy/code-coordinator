"""Byte-budget guard for CLAUDE.md (#2817).

CLAUDE.md is loaded into every worker leg, every review leg, and every
coordinator session -- and re-read on every turn of each, at cache-read
rates. It had already been cut twice for size (#2195: 41,506 -> 24,219
bytes; #2787: ~29,732 -> 19,622 bytes) because nothing watched its size in
between, and it drifted back up at roughly 500 bytes/day both times.
#3422 cut it a third time (18,605 -> 15,001) by moving the `## Commands`
reference, the explanatory Key-Design bullets and the review-prompt-assembly
note out to docs/ -- the first cut made with this guard already in place, so
the cap came down with the file rather than leaving room to drift back into.

This test is that watch. It is deliberately dumb: a byte count, nothing
more -- no parsing, no section analysis, no escape hatch. See #2817 for the
full history and rationale.
"""

from __future__ import annotations

from pathlib import Path

# ~6% headroom over the 15,001-byte size CLAUDE.md was at immediately after
# the #3422 operator-split. Raising this constant is a deliberate, reviewable act
# -- not something to do reflexively to make this test pass. If CLAUDE.md
# has grown past the cap, the fix is almost always to MOVE a section to
# docs/ (leaving a one-line pointer behind), not to raise this number. Per
# the file's own scope note: "if a new rule does not change what a worker
# does, it belongs in docs/."
CLAUDE_MD_MAX_BYTES = 15872  # 15.5 KiB

CLAUDE_MD_PATH = Path(__file__).resolve().parent.parent / "CLAUDE.md"


def test_claude_md_stays_within_byte_budget() -> None:
    size = CLAUDE_MD_PATH.stat().st_size
    assert size <= CLAUDE_MD_MAX_BYTES, (
        f"CLAUDE.md is {size} bytes, over the {CLAUDE_MD_MAX_BYTES}-byte "
        f"(~{CLAUDE_MD_MAX_BYTES / 1024:.1f} KiB) budget.\n\n"
        "CLAUDE.md is loaded into every worker leg, every review leg, and "
        "every coordinator session -- and re-read on every turn of each. "
        "Its size is a direct, per-turn, fleet-wide cost, not a one-time "
        "reading-time cost.\n\n"
        "This file has already been cut twice for exactly this reason "
        "(#2195, #2787, #3422) and drifted back up each time because nothing "
        "enforced a cap -- that is what this test is for.\n\n"
        "The fix is almost always to MOVE a section out to docs/ (leaving "
        "a short pointer line in CLAUDE.md), per the file's own scope "
        "rule: 'if a new rule does not change what a worker does, it "
        "belongs in docs/.' Do NOT raise CLAUDE_MD_MAX_BYTES in this test "
        "as a shortcut -- that defeats the point of the guard. If growth "
        "is truly unavoidable, raising the constant must be its own "
        "deliberate, reviewed decision, not an incidental fix-up. See "
        "#2195, #2787, and #2817 for the full history."
    )
