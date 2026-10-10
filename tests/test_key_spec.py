"""Unit tests for the shared key-spec grammar (#3639)."""

from __future__ import annotations

import pytest

from coord.key_spec import (
    KeyChord,
    KeyEvent,
    KeySpecError,
    UnsupportedKey,
    parse_key_spec,
)


def _chord(modifiers, base, is_char):
    return KeyChord(modifiers=frozenset(modifiers), base=base, is_char=is_char)


class TestNamedKeys:
    def test_bare_named_key(self):
        assert parse_key_spec("enter") == KeyEvent((_chord([], "enter", False),))

    def test_case_insensitive(self):
        assert parse_key_spec("ENTER") == KeyEvent((_chord([], "enter", False),))
        assert parse_key_spec("Esc") == KeyEvent((_chord([], "esc", False),))

    def test_aliases_normalize(self):
        assert parse_key_spec("return") == KeyEvent((_chord([], "enter", False),))
        assert parse_key_spec("escape") == KeyEvent((_chord([], "esc", False),))

    def test_delete_and_backspace_are_different_keys(self):
        delete = parse_key_spec("delete").chords[0]
        backspace = parse_key_spec("backspace").chords[0]
        assert delete.base == "delete"
        assert backspace.base == "backspace"
        assert delete != backspace

    def test_all_function_keys_f1_through_f24(self):
        for n in range(1, 25):
            event = parse_key_spec(f"f{n}")
            assert event.chords[0].base == f"f{n}"

    def test_arrows_home_end_pageup_pagedown_insert(self):
        for name in ("up", "down", "left", "right", "home", "end", "pageup", "pagedown", "insert"):
            event = parse_key_spec(name)
            assert event.chords[0].base == name
            assert event.chords[0].is_char is False

    def test_f25_is_not_a_valid_named_key(self):
        # The grammar caps named function keys at f24 (`NAMED_KEYS`) — f25
        # is neither a named key nor a single character, so it fails to
        # parse at all. This is a grammar-level cap, so it belongs here
        # rather than duplicated through any one driver (a nit from the
        # #3639 review).
        with pytest.raises(KeySpecError):
            parse_key_spec("f25")


class TestModifiers:
    def test_single_modifier_combo(self):
        assert parse_key_spec("alt+m") == KeyEvent((_chord(["alt"], "m", True),))
        assert parse_key_spec("ctrl+c") == KeyEvent((_chord(["ctrl"], "c", True),))
        assert parse_key_spec("shift+right") == KeyEvent((_chord(["shift"], "right", False),))

    def test_option_is_an_alias_for_alt(self):
        assert parse_key_spec("option+m") == parse_key_spec("alt+m")
        assert parse_key_spec("opt+m") == parse_key_spec("alt+m")

    def test_cmd_super_meta_are_the_same_modifier(self):
        base = parse_key_spec("cmd+c")
        assert parse_key_spec("super+c") == base
        assert parse_key_spec("meta+c") == base
        assert parse_key_spec("command+c") == base
        assert parse_key_spec("win+c") == base
        assert parse_key_spec("windows+c") == base

    def test_control_is_an_alias_for_ctrl(self):
        assert parse_key_spec("control+c") == parse_key_spec("ctrl+c")

    def test_multi_modifier_combo_any_order(self):
        a = parse_key_spec("ctrl+shift+p")
        b = parse_key_spec("shift+ctrl+p")
        assert a.chords[0].modifiers == b.chords[0].modifiers == frozenset({"ctrl", "shift"})
        assert a.chords[0].base == b.chords[0].base == "p"

    def test_three_modifiers(self):
        event = parse_key_spec("cmd+option+f")
        chord = event.chords[0]
        assert chord.modifiers == frozenset({"cmd", "alt"})
        assert chord.base == "f"

    def test_case_insensitive_modifiers(self):
        assert parse_key_spec("Alt+M").chords[0].modifiers == frozenset({"alt"})
        assert parse_key_spec("CTRL+SHIFT+P".lower()).chords[0].modifiers == frozenset({"ctrl", "shift"})

    def test_modifier_with_named_key(self):
        event = parse_key_spec("ctrl+shift+right")
        chord = event.chords[0]
        assert chord.modifiers == frozenset({"ctrl", "shift"})
        assert chord.base == "right"
        assert chord.is_char is False

    def test_modifier_with_function_key(self):
        event = parse_key_spec("shift+f3")
        chord = event.chords[0]
        assert chord.modifiers == frozenset({"shift"})
        assert chord.base == "f3"
        assert chord.is_char is False


class TestCasePreservation:
    def test_bare_uppercase_letter_preserves_case(self):
        assert parse_key_spec("M").chords[0].base == "M"
        assert parse_key_spec("m").chords[0].base == "m"

    def test_modifier_plus_uppercase_base_preserves_case(self):
        chord = parse_key_spec("alt+M").chords[0]
        assert chord.base == "M"
        assert chord.modifiers == frozenset({"alt"})


class TestPunctuation:
    @pytest.mark.parametrize(
        "ch", [":", "@", "<", "$", ".", "`", "\\", "[", "]", ";", "'", ",", "/", "-", "=", "_"]
    )
    def test_bare_punctuation_character(self, ch):
        event = parse_key_spec(ch)
        assert event.chords[0] == _chord([], ch, True)

    def test_punctuation_with_modifier(self):
        chord = parse_key_spec("ctrl+[").chords[0]
        assert chord.modifiers == frozenset({"ctrl"})
        assert chord.base == "["
        assert chord.is_char is True

    def test_literal_plus_sign(self):
        assert parse_key_spec("+") == KeyEvent((_chord([], "+", True),))

    def test_bare_single_space_means_the_space_key(self):
        # #3666: a literal `key: ' '` must parse the same as `key: space` —
        # 23 existing vimcode specs were written this way before this
        # grammar existed, and that corpus isn't ours to migrate.
        assert parse_key_spec(" ") == parse_key_spec("space")
        assert parse_key_spec(" ").chords[0] == _chord([], "space", False)

    def test_modifier_plus_literal_plus_sign_is_malformed(self):
        # "ctrl++"  splits into ["ctrl", "", ""] — ambiguous, not supported.
        with pytest.raises(KeySpecError):
            parse_key_spec("ctrl++")


class TestChords:
    def test_two_key_chord_sequence(self):
        event = parse_key_spec("ctrl+k ctrl+w")
        assert len(event.chords) == 2
        assert event.chords[0] == _chord(["ctrl"], "k", True)
        assert event.chords[1] == _chord(["ctrl"], "w", True)

    def test_chord_sequence_order_preserved(self):
        event = parse_key_spec("g g")
        assert event.chords[0].base == "g"
        assert event.chords[1].base == "g"

    def test_three_key_chord(self):
        event = parse_key_spec("ctrl+x ctrl+s enter")
        assert [c.base for c in event.chords] == ["x", "s", "enter"]

    def test_extra_whitespace_between_chords_is_tolerated(self):
        event = parse_key_spec("ctrl+k  ctrl+w")
        assert len(event.chords) == 2


class TestSpaceDeliveredAsRealBytes:
    """#3666: a `key: ' '` must actually deliver a space, not just parse
    without raising. ``parse_key_spec(" ") == parse_key_spec("space")``
    (asserted above) already guarantees every driver's encoder — being a
    pure function of the parsed :class:`KeyChord` — treats the two
    identically; this confirms the tui-pty encoder's actual terminal byte
    is 0x20 for both spellings.

    This exercises :func:`coord.tui_pty_driver.encode_key` directly, one
    layer above the ``SmokeRunner``/YAML spec-step path that actually
    broke in the field (23 vimcode ``tui.yaml`` steps) — the full,
    spec-driven, end-to-end regression test for that path lives in
    ``tests/test_tui_pty_driver.py`` (``TestSmokeRunnerActions``), which is
    also where the Test stage routes for a ``coord/tui_pty_driver.py``
    diff."""

    def test_tui_pty_encodes_bare_space_as_0x20(self):
        from coord.tui_pty_driver import encode_key

        assert encode_key(" ") == b" " == bytes([0x20])
        assert encode_key(" ") == encode_key("space")

    def test_tui_pty_encodes_bare_space_in_a_chord_sequence(self):
        from coord.tui_pty_driver import encode_key_sequence

        assert encode_key_sequence(" ") == [b" "]


class TestErrors:
    def test_empty_string_raises(self):
        with pytest.raises(KeySpecError):
            parse_key_spec("")

    def test_whitespace_only_raises(self):
        with pytest.raises(KeySpecError):
            parse_key_spec("   ")

    def test_two_or_more_bare_spaces_still_raises(self):
        # The single-space exception (#3666) is narrow: it does not mean
        # "strip whitespace". Two+ spaces are still ambiguous with the
        # chord separator and still raise, same as before #3666 — the
        # same shape as test_whitespace_only_raises above, just pinned
        # down to the exact boundary of the new carve-out.
        with pytest.raises(KeySpecError, match="empty key spec"):
            parse_key_spec("  ")

    def test_non_string_raises(self):
        with pytest.raises(KeySpecError):
            parse_key_spec(None)  # type: ignore[arg-type]

    def test_unrecognized_modifier_raises(self):
        with pytest.raises(KeySpecError, match="unrecognized modifier"):
            parse_key_spec("foo+m")

    def test_unrecognized_multichar_base_raises(self):
        with pytest.raises(KeySpecError, match="unrecognized key"):
            parse_key_spec("moonwalk")

    def test_trailing_plus_raises(self):
        with pytest.raises(KeySpecError):
            parse_key_spec("ctrl+")

    def test_leading_plus_raises(self):
        with pytest.raises(KeySpecError):
            parse_key_spec("+c")

    def test_unsupported_key_is_a_distinct_exception_type(self):
        # UnsupportedKey is raised by DRIVER encoders, never by the shared
        # parser itself — just confirm the two error types are distinct so
        # a caller can tell "didn't parse" from "parsed but undeliverable".
        assert not issubclass(UnsupportedKey, KeySpecError)
        assert not issubclass(KeySpecError, UnsupportedKey)

    def test_unsupported_key_message_names_platform_and_key(self):
        err = UnsupportedKey("tui-pty", "cmd+c", "terminals have no Cmd/Super concept")
        assert "tui-pty" in str(err)
        assert "cmd+c" in str(err)
        assert "terminals have no Cmd/Super concept" in str(err)
