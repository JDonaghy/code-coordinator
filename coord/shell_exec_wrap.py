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

#: Matches one leading ``VAR=value`` assignment (plus its trailing
#: whitespace) at the front of a shell simple command — the shape
#: :func:`wrap_launch_command` must convert to an ``env VAR=value`` argument
#: *before* the ``exec`` prefix, because POSIX's assignment-prefix parsing
#: (where a shell applies ``VAR=value`` only to the one command it
#: precedes) does not apply to ``exec``'s own argument list: ``exec
#: FOO=bar ./binary`` tries — and fails — to execve a program literally
#: named ``FOO=bar``. ``env`` has no such restriction (it's a real
#: executable, not a builtin with special parsing), and `/usr/bin/env`
#: itself ``execve()``s straight into its target rather than forking, so
#: ``exec env FOO=bar ./binary`` still collapses to one process. Captured
#: as two groups — name and raw (unquoted) value — so
#: :func:`wrap_launch_command` can re-quote the value via
#: :func:`_quote_env_value` (#3629) before handing it to ``env``.
_ENV_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(\S*)[ \t]+")


def _quote_env_value(value: str) -> str:
    """Double-quote *value* for use as a bare ``env VAR=value`` argument
    (#3629: a leading ``VAR=value`` assignment loses POSIX's
    assignment-prefix exemption from word-splitting the moment it becomes
    a plain argument to ``env`` instead of a real assignment-prefix on the
    shell's own command — so an unquoted ``$PWD`` that *expands* to a path
    containing a space (every macOS worktree, under ``~/Library/Application
    Support/...``) gets split into multiple ``env`` arguments, and ``env``
    then tries — and fails — to execve the stray fragment after the space
    as a command (``env: Support/coord/...: No such file or directory``).

    Double-quoting the value restores the no-split guarantee: parameter
    expansion (``$PWD``) still happens, but the result is kept as one shell
    word regardless of what it expands to. ``\\``, ``"`` and `` ` `` are
    backslash-escaped first so an embedded one of those can't prematurely
    close the quote or trigger command substitution.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
    return f'"{escaped}"'


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
    shape) — :data:`_ENV_ASSIGNMENT_RE` peels those off and re-emits them as
    ``env``'s own arguments instead of ``exec``'s, so the result is ``exec
    env VAR=val ... binary args`` rather than the broken ``exec VAR=val ...
    binary args`` (see :data:`_ENV_ASSIGNMENT_RE` for why). Each value is
    re-emitted double-quoted (:func:`_quote_env_value`, #3629) so a ``$VAR``
    reference that expands to a value containing whitespace — e.g. ``$PWD``
    under a macOS ``~/Library/Application Support/...`` worktree — stays one
    ``env`` argument instead of being word-split into several.

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
        m = _ENV_ASSIGNMENT_RE.match(tail)
        if not m:
            break
        name, value = m.group(1), m.group(2)
        assignments.append(f"{name}={_quote_env_value(value)}")
        tail = tail[m.end():]

    env_prefix = f"env {' '.join(assignments)} " if assignments else ""
    wrapped_tail = f"{EXEC_PREFIX}{env_prefix}{tail}"
    return f"{head} && {wrapped_tail}" if head else wrapped_tail
