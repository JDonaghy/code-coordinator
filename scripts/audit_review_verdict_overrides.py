"""Audit historical reviews for a silently-rewritten verdict (#1456).

Before #1456, the #476 approve-with-nits gate in `coord/auto_loop.py` treated
an *unparseable* blocking count (`None`) as "zero blocking findings", so a
reviewer's well-formed `request-changes` could be rewritten in place to
`approve` and the work marked merge-ready — the only fail-OPEN defect in the
2026-07-26 sweep (observed on #1445).  Nothing recorded that the override
happened: only the final value was stored.

This script recovers those cases from the one place the reviewer's own verdict
survived — the `review_findings` JSON blob, which carries the verdict as
parsed straight out of the reviewer's log (`{"verdict": ..., "body": ...}`)
alongside the assignments row's effective `review_verdict`.  A row where the
blob says `request-changes` but the column says `approve`, with no
`review_verdict_original` recorded, is a pre-#1456 silent override.

Read-only: runs SELECTs against `~/.coord/coord.db` and prints a report.  It
writes nothing and dispatches nothing.

    .venv/bin/python scripts/audit_review_verdict_overrides.py
    .venv/bin/python scripts/audit_review_verdict_overrides.py --db /path/to/coord.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# Rejection language in a review body whose blocking section the heuristic
# cannot read.  Only used for the weaker "suspect" classification, where the
# findings blob carries no verdict of its own.
_REJECTION_MARKERS: tuple[str, ...] = (
    "request-changes", "requesting changes", "request changes",
    "must fix", "must-fix", "blocking", "before merge", "cannot approve",
)


@dataclass(frozen=True)
class Verdict:
    """Classification of one review row."""

    level: str      # "override" | "suspect" | "clean"
    detail: str


def classify_review_row(
    *,
    review_verdict: str | None,
    review_findings: str | None,
    review_verdict_original: str | None,
    assignment_id: str | None = None,
) -> Verdict:
    """Classify one assignments row.  Pure — no DB, no I/O.

    - ``override``: the reviewer's own verdict (as stored in the
      `review_findings` blob) was `request-changes` while the effective
      `review_verdict` is `approve`, and no override was recorded.  Post-#1456
      overrides carry `review_verdict_original`, so they classify as ``clean``
      here — they are audited, which is the point.
    - ``suspect``: effective verdict is `approve`, the blob carries no verdict
      of its own, and the body reads like a rejection whose blocking section
      the heuristic cannot confirm empty.  Needs a human read.
    - ``clean``: everything else.
    """
    if review_verdict != "approve":
        return Verdict("clean", "effective verdict is not approve")
    if review_verdict_original:
        return Verdict(
            "clean",
            f"override already recorded (reviewer said {review_verdict_original!r})",
        )

    body = ""
    blob_verdict: str | None = None
    if review_findings:
        try:
            blob = json.loads(review_findings)
        except (json.JSONDecodeError, TypeError):
            blob = None
        if isinstance(blob, dict):
            blob_verdict = blob.get("verdict")
            body = blob.get("body") or ""
        elif isinstance(review_findings, str):
            body = review_findings

    if blob_verdict == "request-changes":
        return Verdict(
            "override",
            "review_findings blob says request-changes but the row says approve "
            "— pre-#1456 silent rewrite",
        )

    if not body:
        return Verdict("clean", "no findings body stored")

    # Weaker signal: no verdict in the blob, so lean on the body.  A body whose
    # blocking section is confirmed empty is exactly the shape #476 is allowed
    # to advance, so it is not suspicious even if it uses the word "blocking".
    #
    # This calls the PURE predicate, never `record_unparseable_treated_as_blocking`
    # (#3838) — a historical audit scan replaying this over every `approve`
    # row must not inflate the fleet's live fail-closed counter; that counter
    # is incremented only at the real `coord/auto_loop.py` decision site.
    from coord.review import blocking_findings_confirmed_absent  # noqa: PLC0415

    if blocking_findings_confirmed_absent(body, assignment_id=assignment_id):
        return Verdict("clean", "blocking section explicitly empty")
    low = body.lower()
    hits = [m for m in _REJECTION_MARKERS if m in low]
    if hits:
        return Verdict(
            "suspect",
            "approve, but the body reads like a rejection and its blocking "
            f"section is unreadable (markers: {', '.join(hits)})",
        )
    return Verdict("clean", "no rejection language in body")


def scan(db_path: Path) -> list[tuple[sqlite3.Row, Verdict]]:
    """Return the non-clean rows, worst first."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(assignments)")}
    original_col = (
        "review_verdict_original" if "review_verdict_original" in cols else "NULL"
    )
    rows = conn.execute(
        "SELECT assignment_id, repo_name, issue_number, machine_name, "
        "       review_of_assignment_id, review_verdict, review_findings, "
        f"      {original_col} AS review_verdict_original "
        "FROM assignments WHERE review_verdict = 'approve'"
    ).fetchall()
    conn.close()

    out: list[tuple[sqlite3.Row, Verdict]] = []
    for row in rows:
        verdict = classify_review_row(
            review_verdict=row["review_verdict"],
            review_findings=row["review_findings"],
            review_verdict_original=row["review_verdict_original"],
            assignment_id=row["assignment_id"],
        )
        if verdict.level != "clean":
            out.append((row, verdict))
    out.sort(key=lambda pair: (pair[1].level != "override", pair[0]["issue_number"]))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path.home() / ".coord" / "coord.db",
        help="path to coord.db (default: ~/.coord/coord.db)",
    )
    args = parser.parse_args()
    if not args.db.exists():
        print(f"no such database: {args.db}")
        return 2

    findings = scan(args.db)
    if not findings:
        print(f"{args.db}: no silently-overridden review verdicts found.")
        return 0

    overrides = sum(1 for _, v in findings if v.level == "override")
    print(f"{args.db}: {overrides} silent override(s), "
          f"{len(findings) - overrides} suspect row(s)\n")
    for row, verdict in findings:
        print(
            f"[{verdict.level.upper():8}] {row['repo_name']}#{row['issue_number']} "
            f"review={row['assignment_id']} work={row['review_of_assignment_id']}\n"
            f"           {verdict.detail}"
        )
    print(
        "\nEach OVERRIDE row is work the board calls approved that its reviewer "
        "rejected.\nRe-read the review comment on the issue before trusting the "
        "merge state."
    )
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
