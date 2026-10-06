"""Shared ``exec``-wrapping string transform for every driver that runs a
caller-supplied launch command via ``subprocess.Popen(command, shell=True)``
and then needs the returned ``Popen.pid`` to BE the real binary's own pid —
not some intermediate ``/bin/sh`` that forked a grandchild for it (#2096
"one question, one answer": this is the one place that question — "how do I
make a `shell=True` launch collapse into a single process?" — is answered,
rather than each driver growing its own independently-drifting copy of the
same regex-and-string-surgery).

Originally written for :mod:`coord.tui_pty_driver`'s ``UnixPtyChild`` (#3583:
a bare ``Popen("sleep 60", shell=True, preexec_fn=...)`` leaves TWO
processes on a `dash`-backed ``/bin/sh`` — the shell itself, which the
``preexec_fn``/``PR_SET_PDEATHSIG`` armed, and a separate grandchild that
never had a death signal armed on it at all, since `PR_SET_PDEATHSIG` is
cleared on every ``fork()``). Reused as-is by
:class:`coord.mac_native_driver.MacOSCalls` (#3622: the exact same compound
``cd .smoke && HOME=$PWD/home ../target/release/vimcode sample.txt`` shape
left the tracked pid pointing at ``/bin/sh`` instead of the real ``vimcode``
process, so `find_top_window`'s ``CGWindowOwnerPID`` check — and every
pid-addressed input call, ``CGEventPostToPid`` — never matched the real
window-owning process).

``exec`` is the POSIX shell builtin that replaces the shell's own process
image via ``execve()`` instead of forking — this collapses a `shell=True`
launch down to one process (the real binary, same pid throughout) for the
common case, regardless of which OS-level mechanism a caller then uses that
pid for (a Linux parent-death signal, a macOS ``CGWindowOwnerPID``/
``CGEventPostToPid`` lookup, or anything else).
"""

from __future__ import annotations

import re

#: Prefix :func:`wrap_launch_command` adds to the *final simple command* of
#: the caller's launch command. Measured directly against a real ``/bin/sh``
#: (``dash`` on Linux): ``Popen("sleep 60", shell=True)`` leaves TWO
#: processes — the shell itself as a *parent*, and a separate forked
#: ``sleep`` grandchild. Neither `dash` nor macOS's `/bin/sh` (bash in
#: posix/sh-emulation mode) applies an automatic "tail call" exec
#: optimization for a `-c` simple command on their own, so without this,
#: the returned `Popen.pid` is never the real binary's own pid.
EXEC_PREFIX = "exec "

#: Matches one leading ``VAR=`` assignment *name* (and its ``=``) at the
#: front of a shell simple command — the shape :func:`wrap_launch_command`
#: must convert to an ``env VAR=value`` argument *before* the ``exec``
#: prefix, because POSIX's assignment-prefix parsing (where a shell applies
#: ``VAR=value`` only to the one command it precedes) does not apply to
#: ``exec``'s own argument list: ``exec FOO=bar ./binary`` tries — and
#: fails — to execve a program literally named ``FOO=bar``. ``env`` has no
#: such restriction (it's a real executable, not a builtin with special
#: parsing), and `/usr/bin/env` itself ``execve()``s straight into its
#: target rather than forking, so ``exec env FOO=bar ./binary`` still
#: collapses to one process.
#:
#: Only the *name* is captured here — the *value* is deliberately NOT
#: matched by ``\S*`` (#3629 review round 1: that can't see quoting, so it
#: mis-splits a value like ``"a b"`` mid-quote). It is instead consumed as
#: a quote-aware shell word by :func:`_consume_shell_word`, the same way
#: :func:`find_last_top_level_and` tracks quote state.
_ENV_ASSIGNMENT_NAME_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")


def _consume_shell_word(text: str) -> tuple[str, str, bool]:
    """Consume one whitespace-delimited shell word from the front of
    *text*, tracking quote/escape state the same way
    :func:`find_last_top_level_and` does, so a literal space *inside* a
    quoted or backslash-escaped span (``"a b"``, ``a\\ b``) is not mistaken
    for the word's end.

    Returns ``(word, rest, had_separator)``: *word* is the raw text
    consumed (quoting/escaping left completely intact — this function only
    decides *where* the word ends, never what it means), *rest* is
    whatever followed the separating whitespace (with that whitespace
    stripped), and *had_separator* is ``False`` when the word ran all the
    way to the end of *text* without ever finding an unquoted,
    unescaped whitespace character — e.g. an unbalanced quote that
    swallows the remainder of the command. Callers must check it: there is
    no real assignment-prefix without a command after it to run.
    """
    in_single = in_double = escaped = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if escaped:
            escaped = False
        elif ch == "\\" and not in_single:
            escaped = True
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double and ch in " \t":
            break
        i += 1
    had_separator = i < n
    word = text[:i]
    rest = text[i:].lstrip(" \t") if had_separator else ""
    return word, rest, had_separator


def _render_env_value(raw: str) -> str:
    """Render *raw* — the exact shell-syntax text of an env assignment's
    value, as written by the caller and consumed verbatim by
    :func:`_consume_shell_word` — for use as a bare ``env VAR=value``
    argument (#3629).

    If *raw* already contains any shell quoting or escaping of its own
    (``"``, ``'`` or ``\\``), it is passed through completely unchanged:
    the caller already wrote it as a valid, self-contained shell word
    (``"$PWD/home"``, ``'$PWD/home'``, ``"a b"``, ``a\\ b``), and ordinary
    shell quote/escape rules apply to it exactly as they did before this
    function existed — re-quoting it would double-escape characters that
    already mean something (review round 1: this was the actual bug —
    ``HOME="$PWD/home"``, the workaround #3629's own evidence says every
    affected host already deployed, became the broken
    ``HOME="\\"$PWD/home\\""``).

    Otherwise — a plain unquoted, unescaped value such as ``$PWD/home`` —
    it is wrapped in double quotes so a ``$VAR`` reference that expands to
    a value containing whitespace stays one ``env`` argument instead of
    being word-split (the original #3629 bug). Parameter expansion
    (``$PWD``) still happens inside double quotes, and since this branch's
    *raw* text is guaranteed to contain no quote or backslash character,
    nothing inside it needs escaping to make the added quotes safe.
    """
    if any(c in raw for c in "\"'\\"):
        return raw
    return f'"{raw}"'


def find_last_top_level_and(command: str) -> int | None:
    """Return the string index of the ``&`` that starts the *last*
    top-level ``&&`` operator in *command*, or ``None`` if there isn't one.

    "Top-level" means not inside a single- or double-quoted string (the
    only nesting a real launch command needs to care about today, e.g.
    ``cd .smoke && HOME=$PWD/home ../target/release/vimcode sample.txt``)
    — quote state and backslash escapes are tracked so a literal ``&&``
    living inside a quoted argument is correctly skipped rather than
    mistaken for a command separator. Subshells (``( ... )``) and command
    substitution (``$( ... )``/backticks) are out of scope: no real route
    uses them, and misreading one only costs an extra, harmless shell fork
    on the mis-split segment — it can never leak a process the way a
    missed ``exec`` would.
    """
    in_single = in_double = escaped = False
    last_idx = None
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if escaped:
            escaped = False
        elif ch == "\\" and not in_single:
            escaped = True
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double and ch == "&" and command[i + 1:i + 2] == "&":
            last_idx = i
            i += 1  # skip the second '&' of this pair
        i += 1
    return last_idx


def wrap_launch_command(command: str) -> str:
    """Rewrite *command* so the ``/bin/sh -c`` a ``subprocess.Popen(...,
    shell=True)`` runs always ends by collapsing into the real binary via a
    single ``execve()`` — never a shell forking a grandchild for it — so a
    caller tracking the returned ``Popen.pid`` as the real process's own
    identity (a parent-death signal, a window-ownership lookup, a
    pid-addressed input call, ...) actually gets it. See :data:`EXEC_PREFIX`.

    *command* may be a compound script (``cmd1 && cmd2 && ...``, the real
    ``vimcode`` route's shape: a leading ``cd`` followed by the launch):
    only the **final** simple command is rewritten — everything before the
    last top-level ``&&`` (:func:`find_last_top_level_and`) is left exactly
    as the caller wrote it, since ``exec cd ...`` fails outright (``cd`` is
    a shell builtin, not an executable) and earlier steps genuinely need to
    run as normal shell commands, not replace the shell.

    That final simple command may itself open with one or more *inline
    env-var assignments* (``HOME=$PWD/home ./binary``, also a real route's
    shape) — :data:`_ENV_ASSIGNMENT_NAME_RE` plus :func:`_consume_shell_word`
    peel those off and re-emit them as ``env``'s own arguments instead of
    ``exec``'s, so the result is ``exec env VAR=val ... binary args`` rather
    than the broken ``exec VAR=val ... binary args`` (see
    :data:`_ENV_ASSIGNMENT_NAME_RE` for why). Each value is then rendered via
    :func:`_render_env_value` (#3629): an unquoted value is wrapped in double
    quotes so a ``$VAR`` reference that expands to a value containing
    whitespace — e.g. ``$PWD`` under a macOS ``~/Library/Application
    Support/...`` worktree — stays one ``env`` argument instead of being
    word-split into several; a value the caller already quoted or escaped is
    passed through unchanged instead of being re-quoted on top.

    Idempotent at the whole-*command* level: a caller-supplied command that
    already starts with ``exec `` (leading/trailing whitespace tolerated)
    is returned unchanged rather than double-prefixed or re-split.
    """
    if command.strip().startswith(EXEC_PREFIX):
        return command

    split = find_last_top_level_and(command)
    if split is None:
        head, tail = "", command.strip()
    else:
        head, tail = command[:split].strip(), command[split + 2:].strip()

    assignments: list[str] = []
    while True:
        m = _ENV_ASSIGNMENT_NAME_RE.match(tail)
        if not m:
            break
        value, rest, had_separator = _consume_shell_word(tail[m.end():])
        if not had_separator:
            # Ran off the end of the command without ever finding an
            # unquoted separator — e.g. an unbalanced quote swallowed the
            # rest of the line. There's no command left to run after this
            # "assignment", so it isn't one; leave `tail` untouched and
            # stop peeling (matches the pre-#3629-fix behaviour for this
            # already-pathological shape).
            break
        assignments.append(f"{m.group(1)}={_render_env_value(value)}")
        tail = rest

    env_prefix = f"env {' '.join(assignments)} " if assignments else ""
    wrapped_tail = f"{EXEC_PREFIX}{env_prefix}{tail}"
    return f"{head} && {wrapped_tail}" if head else wrapped_tail
