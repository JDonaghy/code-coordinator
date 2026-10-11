#!/usr/bin/env python3
"""Find and remove fix-history cruft from Rust `//` comments.

Agent-written fixes accumulate comments that narrate history rather than
describe the code: `// #1630 review: ...`, `(#1005)`, "this used to ...",
"pre-#1434". Every one of them is re-read by every later worker that opens
the file. This tool removes them as cheaply as possible, in four steps:

    scan     count flagged comment lines per directory (no tokens)
    strip    mechanical rewrite: drop leading issue tags and parenthetical
             issue refs, delete comments that were nothing but a ref (no tokens)
    rewrite  send each file's remaining flagged comment blocks to a small
             model (`claude -p --model haiku`, no tools, minimal system
             prompt) and apply its keep / delete / replace answers
    verify   prove a diff is comment-only: every changed file must lex to
             the same token stream with plain `//` comments removed

By default only plain `//` comments are touched, and `verify` fails if a doc
comment changes. With `--docs`, `///` / `//!` doc comments are handled too:
fenced code inside them (doctests) is never edited and `verify --docs` still
requires it byte-identical, and a doc block is never emptied (that would
trip `missing_docs`). `/* */` blocks are rare enough to leave to a human.

A reference that marks a workaround to remove when an issue closes is kept,
written as `TODO(#N): ...`; that form is not flagged.

Paths under a `tests/acceptance*` directory or named `acceptance.rs` are
skipped by default: they are sealed suites in the fleet repos.

Usage:
    scripts/comment_cruft.py scan    REPO [--depth 2] [--list] [--json]
    scripts/comment_cruft.py strip   REPO [PATH ...]
    scripts/comment_cruft.py rewrite REPO [PATH ...] [--jobs 4] [--limit N] [--dry-run]
    scripts/comment_cruft.py verify  REPO --base REF [--allow GLOB ...]

`strip` and `rewrite` edit the working tree in place; run them on a clean
branch and finish with `verify --base <branch point>` and `cargo check`.
"""
from __future__ import annotations

import argparse
import bisect
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Patterns

# An issue reference: `#123` or `repo#123`. Two to five digits so `#1` in
# prose and six-digit hex colours (`#282828`) don't match; the lookbehind
# keeps HTML entities (`&#123;`) out.
REF = r"(?<![&\w])(?:[A-Za-z][\w.-]*)?#\d{2,5}\b"
REF_LIST = rf"{REF}(?:\s*(?:[,/;&–-]|and)\s*{REF})*"
REF_RE = re.compile(REF)

# `TODO(#123)` / `FIXME(repo#123)`: the sanctioned load-bearing form.
EXEMPT_RE = re.compile(r"\b(?:TODO|FIXME|HACK|XXX)\((?:[A-Za-z][\w.-]*)?#\d+\)")

HISTORY_RE = re.compile(
    r"\bused to\b|\bno longer\b|\bpreviously\b|\boriginally\b|\bpre-#|\bbefore #|\bafter #"
    r"|\bthis pr\b|\bthe reviewer\b|\badversarial\b|\breview(?:er)? (?:found|flagged|finding|note)"
    r"|\bthe old (?:code|path|behaviou?r)\b|\bwas (?:broken|fixed)\b|\bthe fix for\b",
    re.IGNORECASE,
)

# `#1630 review: text` / `#12, #34: text` / `#99 (follow-up) — text` at the
# start of a comment body.
LEADING_TAG_RE = re.compile(
    rf"^(\s*){REF_LIST}"
    r"(?:\s+(?:review|follow-?up|fix|regression|bug))?"
    r"(?:\s*\([^)]*\))?\s*[:—–-]\s+"
)
# ` (#1005)`, ` (see #883)`, ` (quadraui#12, #13 review)`.
PAREN_REF_RE = re.compile(
    rf"\s*\((?:(?:see|cf\.?|per|from|via|issue|fixed in|regression)\s+)?{REF_LIST}(?:\s+review)?\)"
)
# A body that says nothing but which issue it came from.
REF_ONLY_RE = re.compile(rf"^\s*(?:(?:see|cf\.?|per|issue|regression(?: test)? for)\s+)?{REF_LIST}\s*[.:]?\s*$", re.I)
# A body left with no words after stripping (`//`, `// —`, `// :`).
EMPTY_BODY_RE = re.compile(r"^[\s:—–\-.,;]*$")
BANNER_TAIL_RE = re.compile(r"\s([─═━=\-])\1{2,}\s*$")

# rustfmt's default `max_width`; a replacement may not push a line past it.
MAX_WIDTH = 100

DEFAULT_EXCLUDES = ["*/tests/acceptance*/*", "tests/acceptance*/*", "*acceptance.rs", "target/*", "*/target/*"]


def is_flagged(body: str) -> bool:
    body = EXEMPT_RE.sub("", body)
    return bool(REF_RE.search(body) or HISTORY_RE.search(body))


# ---------------------------------------------------------------------------
# Rust lexer: just enough to tell comments from code from literals.


@dataclass
class Seg:
    kind: str  # code | str | line | doc | block
    start: int
    end: int


_IDENT = re.compile(r"[A-Za-z0-9_]")
_RAW_START = re.compile(r'(?:b|c)?r(#*)"')
_STR_START = re.compile(r'(?:b|c)?"')


def lex(src: str) -> list[Seg]:
    segs: list[Seg] = []
    n = len(src)
    i = 0
    code_start = 0

    def flush(upto: int) -> None:
        if upto > code_start:
            segs.append(Seg("code", code_start, upto))

    while i < n:
        c = src[i]
        prev_ident = i > 0 and bool(_IDENT.match(src[i - 1]))
        if c == "/" and src.startswith("//", i):
            flush(i)
            j = src.find("\n", i)
            j = n if j == -1 else j
            third, fourth = src[i + 2 : i + 3], src[i + 3 : i + 4]
            kind = "doc" if third == "!" or (third == "/" and fourth != "/") else "line"
            segs.append(Seg(kind, i, j))
            i = code_start = j
            continue
        if c == "/" and src.startswith("/*", i):
            flush(i)
            depth, j = 1, i + 2
            while j < n and depth:
                if src.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif src.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            segs.append(Seg("block", i, j))
            i = code_start = j
            continue
        if not prev_ident and c in "bcr":
            m = _RAW_START.match(src, i)
            if m:
                flush(i)
                close = '"' + m.group(1)
                j = src.find(close, m.end())
                j = n if j == -1 else j + len(close)
                segs.append(Seg("str", i, j))
                i = code_start = j
                continue
        if c == '"' or (not prev_ident and c in "bc" and src.startswith('"', i + 1)):
            m = _STR_START.match(src, i)
            if m:
                flush(i)
                j = m.end()
                while j < n and src[j] != '"':
                    j += 2 if src[j] == "\\" else 1
                j = min(j + 1, n)
                segs.append(Seg("str", i, j))
                i = code_start = j
                continue
        if c == "'" or (not prev_ident and c == "b" and src.startswith("'", i + 1)):
            q = i if c == "'" else i + 1
            if src.startswith("\\", q + 1):
                j = q + 1
                while j < n and src[j] != "'":
                    j += 2 if src[j] == "\\" else 1
                end = min(j + 1, n)
            elif q + 2 < n and src[q + 2] == "'":
                end = q + 3
            else:
                end = None  # a lifetime or label
            if end is not None:
                flush(i)
                segs.append(Seg("str", i, end))
                i = code_start = end
                continue
        i += 1
    flush(n)
    return segs


# Rust tokens, maximal munch: identifiers, numbers, multi-char operators,
# then any single other character. Comparing token lists rather than text
# makes `verify` blind to pure layout (rustfmt joining `&[\n]` into `&[]`)
# while `>>` vs `> >` still counts as a change.
_TOKEN_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*|\d[\w.]*"
    r"|>>=|<<=|\.\.=|\.\.\.|::|->|=>|==|!=|<=|>=|&&|\|\||\+=|-=|\*=|/=|%=|\^=|&=|\|=|<<|>>|\.\."
    r"|\S"
)


def _line_starts(src: str) -> list[int]:
    return [0] + [m.end() for m in re.finditer("\n", src)]


def normalize(src: str, docs: bool = False) -> list[str]:
    """The file's token stream with plain `//` comments and all layout
    removed. Literals, doc and block comments stay as verbatim atoms.

    With `docs`, doc-comment prose is dropped too, but fenced code inside a
    doc comment (doctests) stays verbatim."""
    out: list[str] = []
    frozen: set[int] = set()
    starts: list[int] = []
    if docs:
        frozen = {c.line for c in line_comments(src, docs=True) if c.frozen}
        starts = _line_starts(src)
    for seg in lex(src):
        text = src[seg.start : seg.end]
        if seg.kind == "line" or (
            docs and seg.kind == "doc" and bisect.bisect_right(starts, seg.start) - 1 not in frozen
        ):
            continue
        if seg.kind == "code":
            out += _TOKEN_RE.findall(text)
        else:
            out.append(text)
    return out


# ---------------------------------------------------------------------------
# Per-file comment model


@dataclass
class Comment:
    line: int  # 0-based line index
    col: int  # column of the `//`
    text: str  # full comment text from `//` to end of line
    own_line: bool  # nothing but whitespace before it
    prefix: str = "//"  # `//`, or `///` / `//!` for a doc comment
    frozen: bool = False  # a doc-comment fence line or inside one: never edited

    @property
    def body(self) -> str:
        return self.text[len(self.prefix) :]

    @property
    def is_doc(self) -> bool:
        return self.prefix != "//"


@dataclass
class Block:
    comments: list[Comment]

    @property
    def own_line(self) -> bool:
        return self.comments[0].own_line

    @property
    def prefix(self) -> str:
        return self.comments[0].prefix

    @property
    def flagged(self) -> bool:
        return any(not c.frozen and is_flagged(c.body) for c in self.comments)


FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


def _prefix(text: str) -> str:
    if text.startswith("//!"):
        return "//!"
    if text.startswith("///") and not text.startswith("////"):
        return "///"
    return "//"


def line_comments(src: str, docs: bool = False) -> list[Comment]:
    """Plain `//` comments, plus `///` / `//!` doc comments when `docs`."""
    starts = _line_starts(src)
    out = []
    for seg in lex(src):
        if not (seg.kind == "line" or (docs and seg.kind == "doc")):
            continue
        ln = bisect.bisect_right(starts, seg.start) - 1
        col = seg.start - starts[ln]
        own = src[starts[ln] : seg.start].strip() == ""
        text = src[seg.start : seg.end]
        out.append(Comment(ln, col, text, own, _prefix(text)))
    in_fence, prev = False, None
    for c in out:
        if not c.is_doc:
            continue
        if prev is None or c.line != prev.line + 1 or c.prefix != prev.prefix:
            in_fence = False
        if FENCE_RE.match(c.body):
            c.frozen, in_fence = True, not in_fence
        else:
            c.frozen = in_fence
        prev = c
    return out


def blocks(comments: list[Comment]) -> list[Block]:
    out: list[Block] = []
    for c in comments:
        last = out[-1].comments[-1] if out else None
        if (
            last is not None
            and c.own_line
            and last.own_line
            and c.line == last.line + 1
            and c.col == last.col
            and c.prefix == last.prefix
            and c.frozen == last.frozen
        ):
            out[-1].comments.append(c)
        else:
            out.append(Block([c]))
    return out


def _doc_runs(comments: list[Comment]) -> list[list[Comment]]:
    """Maximal runs of own-line doc comments attached to one item."""
    runs: list[list[Comment]] = []
    for c in comments:
        if not (c.is_doc and c.own_line):
            continue
        last = runs[-1][-1] if runs else None
        if last is not None and c.line == last.line + 1 and c.prefix == last.prefix and c.col == last.col:
            runs[-1].append(c)
        else:
            runs.append([c])
    return runs


def settle_doc_edits(comments: list[Comment], edits: dict[int, list[str] | None]) -> None:
    """Turn the edits that touch a doc-comment run into one edit of the whole
    run, in place: tidy the blank doc lines a deletion leaves behind, and
    drop the edits outright if they would leave the item with no docs at all
    (that would trip `missing_docs` and lose the item's description)."""
    for run in _doc_runs(comments):
        if not any(c.line in edits for c in run):
            continue
        prefix = run[0].prefix

        def blank(t: str) -> bool:
            return t[len(prefix) :].strip() == ""

        new: list[tuple[str, bool]] = []
        for c in run:
            if c.line in edits:
                new += [(t, False) for t in edits[c.line] or []]
            else:
                new.append((c.text, c.frozen))
        if all(blank(t) for t, _ in new):
            for c in run:
                edits.pop(c.line, None)
            continue
        tidy: list[tuple[str, bool]] = []
        for t, frozen in new:
            if not frozen and blank(t) and (not tidy or (blank(tidy[-1][0]) and not tidy[-1][1])):
                continue
            tidy.append((t, frozen))
        while tidy and not tidy[-1][1] and blank(tidy[-1][0]):
            tidy.pop()
        edits[run[0].line] = [t for t, _ in tidy]
        for c in run[1:]:
            edits[c.line] = []


def apply_edits(src: str, comments_by_line: dict[int, Comment], edits: dict[int, list[str] | None]) -> str:
    """Apply per-line comment edits.

    `edits[line]` is the list of replacement comment texts (each starting
    `//`) for the comment on that line; `[]` deletes the comment (and the
    whole line if it held nothing else). Trailing comments accept at most one
    replacement text.
    """
    lines = src.split("\n")
    out: list[str] = []
    deleted_at: list[int] = []  # indices in `out` where a whole line vanished
    for i, line in enumerate(lines):
        if i not in edits:
            out.append(line)
            continue
        c = comments_by_line[i]
        new = edits[i] or []
        prefix = line[: c.col]
        if c.own_line:
            if not new:
                deleted_at.append(len(out))
            for t in new:
                out.append(prefix + t)
        else:
            if len(new) > 1:
                raise ValueError(f"line {i + 1}: trailing comment replaced by {len(new)} lines")
            out.append(prefix + new[0] if new else prefix.rstrip())
    eof = out[-1:] == [""]  # the file's final newline, not a blank line to tidy
    tidied = _tidy_blank_lines(out[:-1] if eof else out, deleted_at)
    return "\n".join(tidied + ([""] if eof else []))


def _tidy_blank_lines(lines: list[str], sites: list[int]) -> list[str]:
    """Undo the blank-line damage a deletion can do (rustfmt would reject it):
    a doubled blank line, or a blank line left just inside `{` or before `}`."""
    drop: set[int] = set()

    def blank(j: int) -> bool:
        return 0 <= j < len(lines) and lines[j].strip() == ""

    def opens(j: int) -> bool:
        return j < 0 or lines[j].rstrip().endswith(("{", "(", "["))

    def closes(j: int) -> bool:
        return j >= len(lines) or lines[j].strip().startswith(("}", ")", "]"))

    for k in sites:
        before, after = k - 1, k
        if blank(after) and (blank(before) or opens(before) or closes(after + 1)):
            drop.add(after)
        elif blank(before) and not blank(after) and (opens(before - 1) or closes(after)):
            drop.add(before)
    return [ln for j, ln in enumerate(lines) if j not in drop]


# ---------------------------------------------------------------------------
# Mechanical strip


def strip_body(body: str) -> str | None:
    """Return the cleaned comment body, `None` to delete the comment, or the
    body unchanged."""
    if EXEMPT_RE.search(body) or _SAFETY_RE.search(body):
        return body
    if REF_ONLY_RE.match(body):
        return None
    new = body
    m = LEADING_TAG_RE.match(new)
    if m:
        rest = new[m.end() :]
        if rest[:1].islower():
            rest = rest[0].upper() + rest[1:]
        new = m.group(1) + rest
    new = PAREN_REF_RE.sub("", new)
    if new != body and EMPTY_BODY_RE.match(new):
        return None
    rule = BANNER_TAIL_RE.search(new)
    if rule and len(new) < len(body):  # keep `// ── Title ──────` banners their original width
        new += rule.group(1)[0] * (len(body) - len(new))
    return new


def strip_file(src: str, docs: bool = False) -> tuple[str, int]:
    comments = line_comments(src, docs)
    by_line = {c.line: c for c in comments}
    edits: dict[int, list[str] | None] = {}
    for c in comments:
        if c.frozen:
            continue
        new = strip_body(c.body)
        if new is None:
            edits[c.line] = []
        elif new != c.body:
            edits[c.line] = [c.prefix + new.rstrip()]
    changed = len(edits)
    settle_doc_edits(comments, edits)
    if not edits:
        return src, 0
    return apply_edits(src, by_line, edits), changed


# ---------------------------------------------------------------------------
# Model rewrite

SYSTEM_PROMPT = """\
You clean up comments in Rust source. Policy: a comment describes the code as it is now.

Remove history: issue or PR numbers (#123, repo#123), what the code used to do, what a review \
or reviewer found, which fix introduced something, how a bug was discovered.
Keep every present-tense fact a reader needs: invariants, the reason the code is shaped this \
way, non-obvious constraints, warnings, pointers to other code. Never invent facts. Keep the \
original wording and line breaks where they are already fine: make the smallest edit that \
removes the history, and reflow only the lines you change.
Deleting is rare. Most history comments also say something true about the code as it is: \
why it accepts two values, what a test or scenario proves, what an invariant protects, what a \
section contains. Keep that part, restated in the present tense, and drop only the story. \
Delete a block only when nothing in it would help someone reading the current code.
A reference may stay only when it marks a workaround that must be removed when that issue \
closes; then write it as `// TODO(#N): <what to remove>`.

Blocks marked DOC are `///` or `//!` doc comments, rendered as Markdown API documentation. \
Their replacement lines keep the same prefix, keep intra-doc links such as [`Foo`] intact, and \
still describe the item: replace rather than delete unless the whole block is history.

You get numbered comment blocks, each shown with nearby code for context. For every block \
answer one of:
  {"id": N, "action": "keep"}
  {"id": N, "action": "delete"}            (every sentence is pure history)
  {"id": N, "action": "replace", "lines": ["// ...", "// ..."]}
Replacement lines start with the block's own prefix (`// `, or `/// ` / `//! ` for DOC \
blocks), carry no indentation and are no longer than the block's longest original line. A block marked TRAILING sits after code on the same line: replace it with \
exactly one line or delete it.

Reply with only a JSON object: {"blocks": [ ... ]}"""


@dataclass
class RewriteResult:
    path: str
    blocks: int = 0
    changed: int = 0
    deleted: int = 0
    cost_usd: float = 0.0
    error: str = ""
    new_src: str | None = None
    rejected: list[str] = field(default_factory=list)


def build_prompt(src: str, flagged: list[Block], before: int = 3, after: int = 8) -> str:
    lines = src.split("\n")
    parts = []
    for n, b in enumerate(flagged):
        first, last = b.comments[0].line, b.comments[-1].line
        lo, hi = max(0, first - before), min(len(lines), last + 1 + after)
        tag = ("" if b.own_line else " TRAILING") + (f" DOC({b.prefix})" if b.prefix != "//" else "")
        ctx = []
        for i in range(lo, hi):
            marker = ">" if first <= i <= last else " "
            ctx.append(f"{marker} {lines[i]}")
        parts.append(f"### block {n}{tag}\n" + "\n".join(ctx))
    return (
        "Lines marked `>` hold the comment block; the rest is context and must not be echoed.\n\n"
        + "\n\n".join(parts)
    )


def call_model(prompt: str, model: str) -> tuple[str, float]:
    cmd = [
        "claude", "-p",
        "--model", model,
        "--output-format", "json",
        "--system-prompt", SYSTEM_PROMPT,
        "--tools", "",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    # Thinking roughly tripled the cost in trials and bought nothing for this edit.
    env = {**os.environ, "MAX_THINKING_TOKENS": "0"}
    with tempfile.TemporaryDirectory() as cwd:  # no CLAUDE.md to auto-load
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd, env=env, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    env = json.loads(proc.stdout)
    if env.get("is_error"):
        raise RuntimeError(f"claude -p error: {str(env.get('result'))[:300]}")
    return env.get("result", ""), float(env.get("total_cost_usd") or 0.0)


def parse_answer(text: str) -> list[dict]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in model reply")
    return json.loads(m.group(0))["blocks"]


_SAFETY_RE = re.compile(r"\bSAFETY\b")


def _safety_count(texts) -> int:
    """`// SAFETY:` notes justify `unsafe` blocks; a rewrite may reword one
    but never drop it."""
    return sum(len(_SAFETY_RE.findall(t)) for t in texts)


def valid_line(text: str, prefix: str) -> bool:
    """A replacement line is one comment of exactly the block's kind. It may
    not open or close a doc-comment fence: the model sometimes echoes the
    fence that follows the block, which would shift every doctest line."""
    if "\n" in text or _prefix(text) != prefix or not text.startswith(prefix):
        return False
    return prefix == "//" or not FENCE_RE.match(text[len(prefix) :])


def rewrite_source(src: str, answer_fn, chunk: int = 40, docs: bool = False) -> RewriteResult:
    """Rewrite flagged blocks in `src`. `answer_fn(prompt) -> (reply, cost)`."""
    res = RewriteResult(path="")
    comments = line_comments(src, docs)
    by_line = {c.line: c for c in comments}
    flagged = [b for b in blocks(comments) if b.flagged]
    res.blocks = len(flagged)
    edits: dict[int, list[str] | None] = {}
    for start in range(0, len(flagged), chunk):
        group = flagged[start : start + chunk]
        prompt = build_prompt(src, group)
        for attempt in (1, 2):  # a malformed JSON reply is usually a one-off
            reply, cost = answer_fn(prompt)
            res.cost_usd += cost
            try:
                answers = parse_answer(reply)
                break
            except (ValueError, KeyError) as e:
                if attempt == 2:
                    raise RuntimeError(f"unparseable model reply: {e}") from e
        for ans in answers:
            try:
                b = group[int(ans["id"])]
            except (KeyError, ValueError, IndexError, TypeError):
                res.rejected.append(f"bad id {ans.get('id')!r}")
                continue
            action = ans.get("action")
            if action == "keep":
                continue
            new = [] if action == "delete" else ans.get("lines")
            if isinstance(new, list):  # the model often indents despite being told not to
                new = [t.strip() if isinstance(t, str) else t for t in new]
            if not isinstance(new, list) or not all(isinstance(t, str) and valid_line(t.rstrip(), b.prefix) for t in new):
                res.rejected.append(f"line {b.comments[0].line + 1}: invalid replacement")
                continue
            if not b.own_line and len(new) > 1:
                res.rejected.append(f"line {b.comments[0].line + 1}: multi-line trailing replacement")
                continue
            new = [t.rstrip() for t in new]
            width = max(MAX_WIDTH - b.comments[0].col, max(len(c.text.rstrip()) for c in b.comments))
            if any(len(t) > width for t in new):
                res.rejected.append(f"line {b.comments[0].line + 1}: replacement wider than {width}")
                continue
            if new == [c.text.rstrip() for c in b.comments]:
                continue
            if _safety_count(new) < _safety_count(c.text for c in b.comments):
                res.rejected.append(f"line {b.comments[0].line + 1}: dropped a SAFETY note")
                continue
            res.changed += 1
            res.deleted += not new
            lines = [c.line for c in b.comments]
            edits[lines[0]] = new
            for ln in lines[1:]:
                edits[ln] = []
    settle_doc_edits(comments, edits)
    if edits:
        out = apply_edits(src, by_line, edits)
        if normalize(out, docs) != normalize(src, docs):  # cannot happen if apply_edits is right; cheap guard
            raise RuntimeError("rewrite changed non-comment tokens")
        res.new_src = out
    return res


# ---------------------------------------------------------------------------
# Repo helpers


def rust_files(repo: Path, paths: list[str], excludes: list[str]) -> list[str]:
    args = ["git", "-C", str(repo), "ls-files", "--", *(paths or ["."])]
    files = subprocess.run(args, capture_output=True, text=True, check=True).stdout.split("\n")
    return [
        f for f in files
        if f.endswith(".rs") and not any(fnmatch.fnmatch(f, g) for g in excludes)
    ]


def group_of(path: str, depth: int) -> str:
    parts = path.split("/")[:-1]
    return "/".join(parts[:depth]) or "."


def cmd_scan(a) -> int:
    repo = Path(a.repo)
    counts: Counter[str] = Counter()
    blocks_n: Counter[str] = Counter()
    chars = 0
    listing = []
    for f in rust_files(repo, a.paths, a.exclude):
        src = (repo / f).read_text(errors="replace")
        cs = line_comments(src, a.docs)
        for c in cs:
            if not c.frozen and is_flagged(c.body):
                counts[group_of(f, a.depth)] += 1
                chars += len(c.text)
                if a.list:
                    listing.append(f"{f}:{c.line + 1}: {c.text.strip()}")
        blocks_n[group_of(f, a.depth)] += sum(b.flagged for b in blocks(cs))
    total = sum(counts.values())
    if a.json:
        print(json.dumps({"groups": dict(counts), "blocks": dict(blocks_n), "total": total, "approx_tokens": chars // 4}, indent=2))
        return 0
    if a.list:
        print("\n".join(listing))
        return 0
    w = max([len(g) for g in counts] + [5])
    print(f"{'group':<{w}}  lines  blocks")
    for g, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"{g:<{w}}  {n:5}  {blocks_n[g]:6}")
    print(f"\ntotal flagged lines: {total} (~{chars // 4} tokens), blocks: {sum(blocks_n.values())}")
    return 0


def cmd_strip(a) -> int:
    repo = Path(a.repo)
    files = edits = 0
    for f in rust_files(repo, a.paths, a.exclude):
        p = repo / f
        src = p.read_text()
        new, n = strip_file(src, a.docs)
        if n:
            if normalize(new, a.docs) != normalize(src, a.docs):
                print(f"BUG: strip changed code in {f}; left untouched", file=sys.stderr)
                continue
            p.write_text(new)
            files += 1
            edits += n
    print(f"strip: {edits} comment lines edited in {files} files")
    return 0


def cmd_rewrite(a) -> int:
    repo = Path(a.repo)
    todo = []
    for f in rust_files(repo, a.paths, a.exclude):
        src = (repo / f).read_text()
        if any(b.flagged for b in blocks(line_comments(src, a.docs))):
            todo.append(f)
    if a.limit:
        todo = todo[: a.limit]
    print(f"rewrite: {len(todo)} files with flagged blocks (model={a.model}, jobs={a.jobs})", flush=True)

    def work(f: str) -> RewriteResult:
        p = repo / f
        try:
            r = rewrite_source(p.read_text(), lambda prompt: call_model(prompt, a.model), docs=a.docs)
        except Exception as e:  # noqa: BLE001 -- report and keep going
            r = RewriteResult(path=f, error=str(e))
        r.path = f
        if r.new_src is not None and not a.dry_run:
            p.write_text(r.new_src)
        return r

    total_cost = 0.0
    failed = 0
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        for r in ex.map(work, todo):
            total_cost += r.cost_usd
            if r.error:
                failed += 1
                print(f"  FAIL {r.path}: {r.error}", flush=True)
                continue
            reasons = Counter(re.sub(r"^line \d+: ", "", x) for x in r.rejected)
            note = f" (rejected: {dict(reasons)})" if r.rejected else ""
            print(f"  {r.path}: {r.changed}/{r.blocks} blocks changed, {r.deleted} deleted, ${r.cost_usd:.3f}{note}", flush=True)
    print(f"rewrite: done, {failed} failed, total ${total_cost:.2f}")
    return 1 if failed else 0


def cmd_verify(a) -> int:
    repo = Path(a.repo)
    diff = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-status", "--no-renames", a.base],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    bad = 0
    checked = 0
    for row in diff:
        status, path = row.split("\t", 1)
        if any(fnmatch.fnmatch(path, g) for g in a.allow):
            continue
        if status != "M" or not path.endswith(".rs"):
            print(f"FAIL {path}: status {status}; only modified .rs files are allowed")
            bad += 1
            continue
        old = subprocess.run(
            ["git", "-C", str(repo), "show", f"{a.base}:{path}"], capture_output=True, text=True, check=True
        ).stdout
        new = (repo / path).read_text()
        checked += 1
        if normalize(old, a.docs) != normalize(new, a.docs):
            print(f"FAIL {path}: non-comment tokens changed")
            bad += 1
    print(f"verify: {checked} files compared, {bad} failures")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, paths=True):
        p.add_argument("repo")
        if paths:
            p.add_argument("paths", nargs="*", help="limit to these paths (git pathspecs)")
        p.add_argument("--exclude", action="append", default=list(DEFAULT_EXCLUDES), help="glob to skip")
        p.add_argument(
            "--docs", action="store_true",
            help="also handle `///` / `//!` doc comments (fenced doctest code is never touched)",
        )

    p = sub.add_parser("scan")
    common(p)
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--list", action="store_true", help="print every flagged line")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("strip")
    common(p)
    p.set_defaults(fn=cmd_strip)

    p = sub.add_parser("rewrite")
    common(p)
    p.add_argument("--model", default="haiku")
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--limit", type=int, default=0, help="only the first N files")
    p.add_argument("--dry-run", action="store_true", help="call the model but don't write files")
    p.set_defaults(fn=cmd_rewrite)

    p = sub.add_parser("verify")
    common(p, paths=False)
    p.add_argument("--base", required=True)
    p.add_argument("--allow", action="append", default=[], help="glob of non-.rs paths allowed to change")
    p.set_defaults(fn=cmd_verify)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
