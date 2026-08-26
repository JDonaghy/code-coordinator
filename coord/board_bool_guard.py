"""Producer-side guard against the #632 blank-board class (#748).

SQLite has no native boolean type — a column declared ``INTEGER DEFAULT 0``
and used as a flag (e.g. ``is_interactive``) serializes on the `/board` wire
as a raw JSON ``0``/``1``, not ``true``/``false``.  The Rust side
(``tui/src/app/types.rs`` and, since #1941, its generated ``types/
generated.rs`` sibling — see that file's own header) mirrors DB columns into
typed structs; if a *new* such column is ever typed as a plain ``bool`` there
(instead of going through ``de_bool_from_int_or_bool`` or an equivalent
coercing deserializer), that ONE field fails the parse of the **entire**
``BoardPayload`` and blanks the whole TUI board (#632/#546/#628).

This module is the producer-side (Python) half of the seam check: it reads
the Rust struct definitions as text, finds `bool`-typed fields with no
custom deserializer, and cross-references them against the live SQLite
schema. ``tests/test_board_fixture.py`` wires this up against the real Rust
source (``tui/src/app/types.rs`` concatenated with its generated ``types/
generated.rs`` sibling, #1941) + a freshly-migrated DB so CI goes red the
moment someone adds an unguarded INTEGER-backed bool field.

#1849 added the *other* half of this guard, one level up: the ``/board`` wire
contract is now the explicit DTOs in ``coord/board_schema.py``, and
``tests/test_board_schema.py`` asserts every column in
``board_schema.INTEGER_BACKED_BOOLEANS`` is declared ``int`` — so the JSON
type is pinned by the contract rather than by SQLite's (missing) boolean type,
and a Postgres backend cannot silently start sending ``true``/``false``.  This
text-scraping check stays because it is the only one that reads the *actual
Rust consumer*; the DTO assertion cannot see ``types.rs``.
"""

from __future__ import annotations

import re

# Matches `#[attr] #[attr] ... pub(crate) <name>: bool,` — i.e. a struct
# field typed as a plain `bool` (not `Option<bool>`, not some wrapper type).
# `attrs` captures every `#[...]` attribute line directly above the field so
# we can check it for a `deserialize_with` guard and/or a `rename`.
_BOOL_FIELD_RE = re.compile(
    r"(?P<attrs>(?:[ \t]*#\[[^\]]*\]\s*\n)*)"
    r"[ \t]*pub\(crate\)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*):\s*bool,",
)
_RENAME_RE = re.compile(r'rename\s*=\s*"([^"]+)"')


def find_unguarded_bool_fields(rust_src: str) -> dict[str, str]:
    """Return ``{wire_key: field_name}`` for `bool` struct fields in *rust_src*
    that have no `deserialize_with` attribute.

    Such fields require the wire to send a literal JSON `true`/`false` — a
    SQLite-style `0`/`1` integer fails the whole containing struct's parse.
    """
    out: dict[str, str] = {}
    for m in _BOOL_FIELD_RE.finditer(rust_src):
        attrs = m.group("attrs")
        if "deserialize_with" in attrs:
            continue  # guarded — e.g. de_bool_from_int_or_bool
        name = m.group("name")
        rename = _RENAME_RE.search(attrs)
        wire_key = rename.group(1) if rename else name
        out[wire_key] = name
    return out


def find_integer_bool_mismatches(
    rust_src: str, schema_columns: dict[str, dict[str, str]]
) -> list[str]:
    """Cross-reference unguarded Rust `bool` fields against SQLite column types.

    ``schema_columns`` maps table name -> ``{column_name: declared_type}``
    (as ``PRAGMA table_info`` reports it, e.g. ``"INTEGER"``, ``"TEXT"``).

    Returns ``"<table>.<column>"`` for every unguarded `bool` field whose
    wire key matches a column declared ``INTEGER`` — the exact #632 trigger.
    Empty list means the seam is safe.
    """
    unguarded = find_unguarded_bool_fields(rust_src)
    mismatches: list[str] = []
    for table, cols in sorted(schema_columns.items()):
        for col, decl_type in sorted(cols.items()):
            if col in unguarded and decl_type.upper().startswith("INTEGER"):
                mismatches.append(f"{table}.{col}")
    return mismatches
