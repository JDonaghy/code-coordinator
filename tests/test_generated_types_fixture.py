"""#750/#1550/#2009: the OpenAPI -> TypeScript generator stays correct here,
even though the file it generates now lives in another repo.

Originally (#750/#1550) this mirrored tests/test_board_fixture.py's
freshness-check pattern: the "fixture" was the generated TypeScript wire-type
file `coord/dashboard/webapp/src/api/generated.ts`, and this test asserted the
committed copy matched `scripts/codegen.py`'s output byte-for-byte. A Python
dataclass field added, removed or retyped without regenerating went red here,
closing the hand-mirrored-wire-contract drift class from #750.

#2009 (epic #2002) moved the webapp — and `generated.ts` with it — into the
`coord-web` repo. That byte-comparison is therefore no longer performable in
this checkout: there is no committed TypeScript to compare against. It has
NOT been dropped, it MOVED: `coord-web`'s CI runs
`python -m coord.codegen --check --out src/api/generated.ts` against its own
copy, using the generator from the `code-coordinator[server]` wheel it
already installs (docs/ADR_COORD_WEB_CI.md, #2006; the generator moved from
`scripts/codegen.py` to `coord/codegen.py` in #3045 so that wheel install
can actually reach it — `scripts/` was never shipped).

What is still provable from here, and what this file now pins, is the
*producer* half — the half that lives in this repo and is the half that
changes when a Python dataclass changes:

  - the generator runs at all against the real served spec, and
  - it emits a TypeScript declaration for EVERY schema in that spec, so a
    newly registered dataclass cannot be silently dropped from the wire
    contract on its way out of Python.

Because the spec itself is drift-tested against the real route table
(tests/test_openapi.py, #757), a green run here plus a green `--check` in
`coord-web` still spans the whole original chain.
"""

from __future__ import annotations

import pytest

from coord.dashboard.server import openapi_spec
from scripts.codegen import (
    OUTPUT_ENV_VAR,
    OUTPUT_RELPATH,
    OutputPathError,
    generate,
    resolve_output_path,
)


def test_generator_emits_a_declaration_for_every_served_schema():
    """Nothing in `components/schemas` falls out of the TS mirror."""
    generated = generate()
    schemas = openapi_spec().get("components", {}).get("schemas", {})
    assert schemas, "the served OpenAPI spec declares no schemas at all"
    missing = [
        name
        for name in schemas
        if f"interface {name} " not in generated and f"interface {name}\n" not in generated
    ]
    assert not missing, (
        f"coord/codegen.py emitted no TypeScript interface for {missing} — "
        "a schema registered in coord/openapi.py is not reaching coord-web's "
        "generated.ts, which is the #750 hand-mirrored-contract drift class"
    )


def test_generated_output_is_non_empty_typescript():
    generated = generate()
    assert generated.endswith("\n")
    assert "export interface" in generated


def test_out_flag_names_the_destination(tmp_path, monkeypatch):
    """`--out PATH` wins outright, env or no env."""
    monkeypatch.setenv(OUTPUT_ENV_VAR, str(tmp_path / "from-env"))
    explicit = tmp_path / "explicit" / "generated.ts"
    assert resolve_output_path(explicit) == explicit


def test_env_var_resolves_relative_to_a_coord_web_checkout_root(tmp_path, monkeypatch):
    monkeypatch.setenv(OUTPUT_ENV_VAR, str(tmp_path))
    assert resolve_output_path(None) == tmp_path / OUTPUT_RELPATH


def test_no_destination_is_an_error_not_a_guess(monkeypatch):
    """#2009: the old hard-coded in-repo path must not be resurrected as a
    default. Guessing is always wrong post-split, and under `--check` it is
    wrong in the direction that reports success."""
    monkeypatch.delenv(OUTPUT_ENV_VAR, raising=False)
    with pytest.raises(OutputPathError):
        resolve_output_path(None)
