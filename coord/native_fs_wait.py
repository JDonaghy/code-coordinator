"""Shared filesystem-expectation check for every Tier-2 native driver (#3650).

``expect_file`` is the one step type #3650 calls "the workhorse": type into
the editor + ``:w``, run a command in the terminal panel, install an
extension -> check the real file landed on disk, with the real content. It
is also the one new step with NO platform-specific behaviour at all — a path
either exists (with the expected content, when asked) or it doesn't, and
that's exactly as true on macOS/Windows/Linux/a raw pty. Per CLAUDE.md's "one
question, one answer" rule (epic #2096), that means there must be exactly ONE
implementation of "does this file exist (with this content) within this
deadline" — :func:`wait_for_file` below — called from
:mod:`coord.mac_native_driver`, :mod:`coord.win_native_driver`,
:mod:`coord.gtk_native_driver` and :mod:`coord.tui_pty_driver` alike, rather
than four independently-written (and inevitably drifting) copies the way
``_find_a11y_match``/``_summarize_elements`` are deliberately duplicated
per-driver (those differ in their *source* tree — AX vs UIA vs AT-SPI — this
does not differ at all).

Confirmed by actually re-reading the filesystem on every poll (#2096:
unconfirmed success is a defect) — never by the mere absence of an exception
from an earlier step that was merely supposed to *cause* the file to appear.
"""

from __future__ import annotations

import os
import re
import time

#: The PowerShell ``$env:NAME`` spelling, rewritten to the plain ``$NAME``
#: :func:`os.path.expandvars` already understands — see :func:`_resolve_path`.
_PS_ENV_RE = re.compile(r"\$env:([A-Za-z_][A-Za-z0-9_]*)")


def wait_for_file(
    path: str, timeout_ms: int, contains: str | None = None, *, poll_interval_s: float = 0.05,
) -> tuple[bool, str]:
    """Poll for *path* to exist within *timeout_ms* milliseconds, optionally
    requiring its content to contain the substring *contains*.

    *path* is run through :func:`os.path.expanduser`/:func:`os.path.expandvars`
    first, plus a PowerShell-style ``$env:NAME`` token rewrite (to plain
    ``$NAME``, which :func:`os.path.expandvars` already understands) — so a
    spec author can write either ``$env:TEMP\\probe.txt`` (natural on
    Windows, where the run's working files commonly live under
    ``$env:TEMP``) or the POSIX ``$TMPDIR``/``~`` spellings, and either
    resolves against *this* process's own environment rather than requiring
    the spec to hardcode an absolute path that may differ run to run.

    Returns ``(True, "")`` the instant the file (and, when given, its
    content) matches; ``(False, reason)`` once *timeout_ms* elapses without
    ever matching — reason names exactly what was still missing (the file
    itself, or the expected content) and, when the file did appear but
    without the right content, includes a truncated snippet of what was
    actually there, which is usually enough to tell a timing miss from a
    genuinely wrong write.

    Never raises for an ordinary "not there yet"/"wrong content" outcome —
    only for *path* resolving to something unreadable as a regular file
    despite existing (e.g. a directory), which is squarely a malformed-spec
    condition the caller should surface as a failing step, not silently poll
    forever against.
    """
    resolved = _resolve_path(path)
    deadline = time.monotonic() + timeout_ms / 1000
    last_reason = f"{resolved!r} did not appear within {timeout_ms}ms"
    while True:
        exists = os.path.exists(resolved)
        if exists:
            if os.path.isdir(resolved):
                return False, f"{resolved!r} exists but is a directory, not a file"
            if contains is None:
                return True, ""
            try:
                with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError as e:
                last_reason = f"{resolved!r} exists but could not be read: {e}"
            else:
                if contains in text:
                    return True, ""
                snippet = text if len(text) <= 200 else text[:200] + "…"
                last_reason = (
                    f"{resolved!r} exists but does not contain {contains!r}; "
                    f"actual content: {snippet!r}"
                )
        else:
            last_reason = f"{resolved!r} did not appear within {timeout_ms}ms"
        if time.monotonic() >= deadline:
            return False, last_reason
        time.sleep(poll_interval_s)


def _resolve_path(path: str) -> str:
    """Expand ``~``, POSIX ``$VAR``/``${VAR}`` and the PowerShell
    ``$env:VAR`` spelling against THIS process's own environment. A spec
    author on any platform can write the natural-looking form for that
    platform's temp dir and have it resolve the same way here."""
    rewritten = _PS_ENV_RE.sub(r"$\1", path)
    return os.path.expanduser(os.path.expandvars(rewritten))
