"""The #3182 fan-out's on-the-board *text encodings*, and nothing else.

A capability-partition fan-out records two things as plain text on rows the
board already has, rather than in a new table (see ``coord.smoke``'s own
module note for why): each leg's ``issue_title`` carries a
``[smoke:<caps>]`` tag, and the parent work row's ``test_reason`` carries a
``[[smoke-fanout:...]]`` manifest naming every leg in that round.

Those four functions used to live in ``coord/smoke.py``, next to the
dispatcher that writes them. They are split out here because ``coord.smoke``
imports the whole dispatch stack (``coord.config``, ``coord.dispatch``,
``coord.github_ops``, ``coord.revalidate``, ``httpx``), while the encodings
themselves are pure ``re``/``base64`` string handling with no coord
dependency at all — and ``coord.stage_projection``, which has to *read* both
encodings to count Test rounds (#3191), is deliberately a leaf module
("Pure computation: ... no I/O, no side effects") whose only other
production import is ``coord.models``. Importing ``coord.smoke`` from it
just to reach two regexes would have dragged the entire dispatch stack into
``coord serve``'s ``/board`` request path — a first-request-time import of
half the package inside a threadpool worker — for no benefit.

``coord.smoke`` re-exports every name below under its original spelling, so
existing importers (``coord.notify``, ``coord.diagnose``,
``coord.reconcile``, and the tests) are unaffected and there is exactly one
definition of each encoding.
"""

from __future__ import annotations

import base64
import re

# Restricted to `[a-z0-9+_-]` — every capability name in this codebase
# (gtk, windows, macos, browser, provider:opencode via `+`-joining, etc.)
# fits that class. If `coordinator.yml` ever declares a capability with an
# uppercase letter, a dot, or another character outside it,
# `smoke_leg_capabilities()` silently returns None for that leg (misread as
# an ordinary untagged row) rather than raising — nothing today validates
# capability naming at config-load time, so keep new capability names
# lowercase/`[a-z0-9+_-]` until that validation exists.
_LEG_TAG_RE = re.compile(r"^\[smoke:([a-z0-9+_-]+)\] ")


def smoke_leg_issue_title(base_title: str, capabilities: tuple[str, ...]) -> str:
    """The ``issue_title`` for one capability-partition leg of a #3182 fan-out.

    Encodes *capabilities* (sorted, ``+``-joined) as a parseable prefix —
    ``"[smoke:gtk+windows] <base_title>"`` — so :func:`smoke_leg_capabilities`
    can read it back off the board. Used both for the per-partition in-flight
    dedupe (``coord.smoke._find_leg_for_partition``) and for telling a
    fan-out leg's own verdict apart from an ordinary single-leg smoke row's
    when processing it (``coord.notify``).
    """
    tag = "+".join(sorted(capabilities))
    return f"[smoke:{tag}] {base_title}"


def smoke_leg_capabilities(issue_title: str | None) -> tuple[str, ...] | None:
    """The capability set :func:`smoke_leg_issue_title` encoded, or ``None``.

    ``None`` for an ordinary (untagged) smoke row — every pre-#3182 ``[smoke]
    ...`` single-partition dispatch, which never carries this prefix — or for
    any non-smoke row. Never raises on a malformed or missing title.
    """
    if not issue_title:
        return None
    m = _LEG_TAG_RE.match(issue_title)
    if not m:
        return None
    return tuple(m.group(1).split("+"))


_FANOUT_MANIFEST_RE = re.compile(r"^\[\[smoke-fanout:([^\]]*)\]\]\n?")


def _encode_fanout_manifest(
    legs: list[tuple[str, tuple[str, ...], str | None]],
) -> str:
    """The manifest line stamped at the FRONT of the parent's ``test_reason``
    for a #3182 fan-out: ``[[smoke-fanout:<id>=<caps>=<command_b64>,...]]``,
    one entry per leg dispatched (or already active/completed) this round.
    Preserved byte-for-byte across every later rewrite of the parent's
    ``test_reason`` (the running-progress stamp, and the final aggregate) so
    ``coord.smoke.finalize_smoke_fanout`` can always find its siblings again
    from just the parent's own row — see that module's note for why this, and
    not a new query endpoint.

    ``command_b64`` (#3298) is the partition's own resolved Test-stage
    command, URL-safe base64-encoded so an arbitrary shell command — commas,
    brackets, newlines, anything a real ``test_command``/rule ``command`` can
    contain — can never corrupt this manifest's own ``,``/``=``/``]``
    delimiters. Encodes as an empty third field when the command is unknown
    (``None``), which round-trips through :func:`_parse_fanout_manifest` as
    ``command=None`` rather than raising or misparsing.
    """
    def _entry(leg_id: str, caps: tuple[str, ...], command: str | None) -> str:
        cap_str = "+".join(sorted(caps))
        cmd_b64 = (
            base64.urlsafe_b64encode(command.encode()).decode() if command else ""
        )
        return f"{leg_id}={cap_str}={cmd_b64}"

    body = ",".join(_entry(leg_id, caps, command) for leg_id, caps, command in legs)
    return f"[[smoke-fanout:{body}]]"


def _parse_fanout_manifest(
    test_reason: str | None,
) -> list[tuple[str, tuple[str, ...], str | None]] | None:
    """The ``(leg_id, capabilities, command)`` triples
    :func:`_encode_fanout_manifest` wrote, or ``None`` when *test_reason*
    carries no manifest (not a fan-out row). Tolerates a malformed entry by
    skipping just that entry, never raising.

    ``command`` is ``None`` for a pre-#3298 two-field entry
    (``<id>=<caps>``, no trailing ``=<command_b64>``) — an already-in-flight
    row from before this field existed — as well as for a malformed base64
    payload; either way the caller gets "unknown command", never a crash.
    """
    if not test_reason:
        return None
    m = _FANOUT_MANIFEST_RE.match(test_reason)
    if not m:
        return None
    legs: list[tuple[str, tuple[str, ...], str | None]] = []
    for entry in m.group(1).split(","):
        if not entry:
            continue
        leg_id, _, rest = entry.partition("=")
        caps, _, cmd_b64 = rest.partition("=")
        if not leg_id or not caps:
            continue
        command: str | None = None
        if cmd_b64:
            try:
                command = base64.urlsafe_b64decode(cmd_b64.encode()).decode()
            except (ValueError, UnicodeDecodeError):
                command = None
        legs.append((leg_id, tuple(caps.split("+")), command))
    return legs
