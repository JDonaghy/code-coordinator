"""One shared key-spec grammar for every `coord app-drive` driver (#3639).

Each of the four native/pty drivers (:mod:`coord.mac_native_driver`,
:mod:`coord.win_native_driver`, :mod:`coord.gtk_native_driver`,
:mod:`coord.tui_pty_driver`) grew its own small ``key:`` parser, and each had
a different gap: the mac/win parsers only knew a fixed named-key table plus
``ctrl+<single char>``, so ``alt+m``/``cmd+c``/``ctrl+shift+p`` all raised
"unrecognized key"; win-native's CLI additionally accepted punctuation like
``:``/``.`` but silently sent nothing; the tui-pty parser only had
Alt/Shift/Ctrl combos for a fixed subset of named keys, so ``shift+f3``
raised too.

This module is the ONE place that answers "what does this ``key:`` string
mean" — :func:`parse_key_spec` turns it into a driver-neutral
:class:`KeyEvent` (a chord sequence). Each driver then has its OWN encoder
that maps a :class:`KeyChord` onto its platform's actual input call (CGEvent
virtual keycodes, Win32 ``SendInput``, ``xdotool`` keysyms, raw terminal
bytes) — that per-platform mapping is NOT shared, since e.g. ``cmd`` means
something to macOS/X11 and means nothing a real terminal can deliver at all.

Grammar
-------
A ``key:`` value is one or more whitespace-separated **chords**, sent in
order (a **chord sequence** — e.g. ``ctrl+k ctrl+w``, a two-key chord
binding). Each chord is::

    [<modifier>+]...<base>

- **modifiers** (any combination, any order, case-insensitive):
  ``ctrl``/``control``; ``alt``/``option``/``opt``; ``shift``;
  ``cmd``/``command``/``super``/``meta``/``win``/``windows``. All four
  spellings in the last group name the SAME physical modifier (macOS calls
  it Cmd, Linux window managers call it Super or Meta, Windows calls it the
  Win key) — there is no separate fifth modifier for "Windows key".
- **base** is either:
  - a **named key**, case-insensitive (:data:`NAMED_KEYS`): ``enter``
    (alias ``return``), ``esc`` (alias ``escape``), ``tab``, ``backspace``,
    ``delete`` (forward-delete — a different physical key from
    ``backspace``, #3627), ``space``, ``up``/``down``/``left``/``right``,
    ``home``, ``end``, ``pageup``, ``pagedown``, ``insert``, ``f1``-``f24``;
  - or **any single printable character**, including punctuation (``:``,
    ``@``, ``<``, ``$``, ``.``, `````, ``\\``, ``[``, ``]``) — case is
    preserved (``key: M`` is Shift+m; ``key: shift+m`` means the same
    thing spelled explicitly).

A literal single space (``" "``, exactly one space character and nothing
else) is special-cased to mean the ``space`` key, same as spelling it out —
#3666: a lot of existing ``key:`` specs (vimcode's ``tui.yaml`` alone has 23)
predate this grammar and were written as ``key: ' '``, and that corpus isn't
this repo's to migrate. This is narrower than "whitespace is a separator,
so strip it": any OTHER spec made of two or more spaces (``"  "``) is still
ambiguous with the chord separator and still raises, same as an empty
string.

A bare ``+`` (the character itself, not a separator) is the other special
case: ``"+"`` parses as the literal plus-sign key with no modifiers, since
splitting it on ``+`` would otherwise produce two empty tokens.

Nothing in this grammar is platform-specific — a parse failure here
(:class:`KeySpecError`) means the STRING itself doesn't parse under the
grammar at all (unknown modifier name, a multi-character base that isn't a
named key, an empty spec). A driver that can parse a chord but genuinely
cannot deliver it on its platform (``cmd`` to a terminal; an F-key with no
platform keycode) raises :class:`UnsupportedKey` instead — never a silent
``{"ok": true}`` no-op (the #3635 bug this issue closes).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "KeyChord",
    "KeyEvent",
    "KeySpecError",
    "UnsupportedKey",
    "NAMED_KEYS",
    "MODIFIERS",
    "parse_key_spec",
]


class KeySpecError(Exception):
    """Raised when a ``key:`` string doesn't parse under the shared grammar
    at all: an empty spec, an unrecognized modifier name, or a base that is
    neither a named key nor a single character."""


class UnsupportedKey(Exception):
    """Raised by a driver's own encoder — never by :func:`parse_key_spec`
    itself — when a key it successfully PARSED genuinely cannot be
    delivered on that platform (``cmd`` to a terminal with no Super/Cmd
    concept; an F-key beyond what the platform has a real keycode for).
    Names the platform and the key so the failure is actionable, and is
    always an exception, never a silently-ignored no-op (#3639, closing
    #3635's ``{"ok": true}`` bug for good across every driver)."""

    def __init__(self, platform: str, key_desc: str, reason: str = "") -> None:
        detail = f" — {reason}" if reason else ""
        super().__init__(f"{platform} cannot deliver key {key_desc!r}{detail}")
        self.platform = platform
        self.key_desc = key_desc


#: Canonical modifier tokens every driver encoder sees in
#: :attr:`KeyChord.modifiers`.
MOD_CTRL = "ctrl"
MOD_ALT = "alt"
MOD_SHIFT = "shift"
MOD_CMD = "cmd"

MODIFIERS: frozenset[str] = frozenset({MOD_CTRL, MOD_ALT, MOD_SHIFT, MOD_CMD})

#: Every spelling a journey/catalogue entry might use for a modifier,
#: normalized to one of :data:`MODIFIERS` (case-insensitive — callers lower
#: the token before looking it up here).
_MODIFIER_ALIASES: dict[str, str] = {
    "ctrl": MOD_CTRL, "control": MOD_CTRL,
    "alt": MOD_ALT, "option": MOD_ALT, "opt": MOD_ALT,
    "shift": MOD_SHIFT,
    "cmd": MOD_CMD, "command": MOD_CMD, "super": MOD_CMD, "meta": MOD_CMD,
    "win": MOD_CMD, "windows": MOD_CMD,
}

#: Named (multi-character) key tokens every driver must recognize,
#: independent of how each platform actually encodes them.
NAMED_KEYS: frozenset[str] = frozenset({
    "enter", "esc", "tab", "backspace", "delete", "space",
    "up", "down", "left", "right", "home", "end", "pageup", "pagedown",
    "insert",
    *(f"f{n}" for n in range(1, 25)),
})

#: Synonyms for a named key, normalized to the one canonical spelling in
#: :data:`NAMED_KEYS` above.
_KEY_ALIASES: dict[str, str] = {
    "return": "enter",
    "escape": "esc",
}


@dataclass(frozen=True)
class KeyChord:
    """One parsed chord: a set of modifiers plus exactly one base.

    ``is_char=False`` means ``base`` is one of :data:`NAMED_KEYS` (already
    canonicalized — ``return``/``escape`` never appear here, only
    ``enter``/``esc``). ``is_char=True`` means ``base`` is a single
    character, case PRESERVED exactly as written (``"M"`` is a different
    chord from ``"m"`` — see :func:`parse_key_spec`'s docstring on implicit
    Shift)."""

    modifiers: frozenset[str]
    base: str
    is_char: bool


@dataclass(frozen=True)
class KeyEvent:
    """One full ``key:`` spec value, parsed: an ordered, non-empty sequence
    of :class:`KeyChord` — length 1 for an ordinary key, length >1 for a
    space-separated chord sequence (``ctrl+k ctrl+w``), sent in that
    order."""

    chords: tuple[KeyChord, ...]


def _parse_chord(token: str) -> KeyChord:
    if not token:
        raise KeySpecError("empty key token")
    if token == "+":
        # The one ambiguous case: '+' is both the modifier separator and a
        # legitimate single-character key. A bare '+' token never looks
        # like "<modifier>+<base>" (it IS the base, with no modifiers), so
        # special-case it before splitting.
        return KeyChord(modifiers=frozenset(), base="+", is_char=True)

    parts = token.split("+")
    if any(part == "" for part in parts):
        raise KeySpecError(
            f"malformed key token {token!r} (stray '+' — use the literal "
            f"token '+' on its own for the plus-sign key)"
        )
    *mod_tokens, base = parts
    modifiers: set[str] = set()
    for mod_tok in mod_tokens:
        canonical = _MODIFIER_ALIASES.get(mod_tok.lower())
        if canonical is None:
            raise KeySpecError(f"unrecognized modifier {mod_tok!r} in key {token!r}")
        modifiers.add(canonical)

    base_lower = base.lower()
    canonical_name = _KEY_ALIASES.get(base_lower, base_lower)
    if canonical_name in NAMED_KEYS:
        return KeyChord(modifiers=frozenset(modifiers), base=canonical_name, is_char=False)
    if len(base) == 1:
        return KeyChord(modifiers=frozenset(modifiers), base=base, is_char=True)
    raise KeySpecError(f"unrecognized key {token!r}")


def parse_key_spec(spec: str) -> KeyEvent:
    """Parse one ``key:`` spec value — a single chord or a space-separated
    chord sequence — into a driver-neutral :class:`KeyEvent`.

    Raises :class:`KeySpecError` for anything that doesn't parse under the
    grammar at all: not a string, empty, made of two or more space
    characters, an unrecognized modifier name, or a base that is neither a
    recognized named key nor a single character. Note this is NOT "any
    whitespace-only string" — a lone tab (``"\\t"``) is not a space
    character, splits into the single token ``"\\t"``, and parses as a
    literal-character chord rather than raising.

    The one exception: a spec that is exactly a single space (``" "``)
    means the ``space`` key (#3666) — see the module docstring. The empty
    string, and any run of two or more spaces (``""``, ``"  "``, ...),
    still raise."""
    if not isinstance(spec, str):
        raise KeySpecError(f"key spec must be a string, got {spec!r}")
    if spec == " ":
        return KeyEvent(chords=(_parse_chord("space"),))
    tokens = [tok for tok in spec.split(" ") if tok]
    if not tokens:
        raise KeySpecError(f"empty key spec {spec!r}")
    return KeyEvent(chords=tuple(_parse_chord(tok) for tok in tokens))
