"""#2900: coord-tui's daemon **write** client is generated, not hand-built.

The read path has been generated and gated since #1941. The write path was
not: coord-tui hand-assembled `serde_json::json!({...})` bodies against ~50
verb-shaped routes. Inside one repo that was a tolerable mirror; since #2899
split the repos it is a cross-repo contract per endpoint, each free to drift
in silence — and a drifted WRITE does not blank a panel the way a drifted
READ does, it records the wrong thing.

What a single checkout can prove — and what this file pins — mirrors
`tests/test_generated_rust_fixture.py`:

  - the generator runs against the real served spec and emits a request type
    for every endpoint coord-tui calls,
  - **a wire change here changes the output** (the property coord-tui's CI
    gate converts into a red build),
  - the two serialization semantics that JSON Schema cannot express are
    emitted correctly: verb routes send an explicit `null`, PATCH routes
    distinguish absent from null, and
  - `X-Coord-Schema` (#1943) is attached to resource-shaped routes and to
    nothing else.

The byte-for-byte comparison against coord-tui's committed file is that
repo's CI job (`docs/ADR_COORD_TUI_CI.md`), asserted from here by
`coord.health.checks.coord_tui_ci_pin`.
"""

from __future__ import annotations

import copy

import pytest

from coord import codegen
from coord.codegen import (
    RUST_OUTPUT_ENV_VAR,
    RUST_REQUESTS_OUTPUT_RELPATH,
    RUST_WRITE_ENDPOINTS,
    OutputPathError,
    WriteEndpoint,
    WriteEndpointError,
    generate_rust_requests,
    resolve_rust_requests_output_path,
)

# ── coverage: every endpoint coord-tui calls has a generated type ────────────


def test_a_request_struct_is_emitted_for_every_declared_endpoint():
    generated = generate_rust_requests()
    for ep in RUST_WRITE_ENDPOINTS:
        assert f"struct {ep.base}Request {{" in generated, (
            f"no request struct for {ep.method.upper()} {ep.path} — coord-tui "
            "would have to hand-build that body again."
        )


def test_the_endpoints_the_tui_actually_posts_to_are_all_covered():
    """The four `post_daemon_json` call sites coord-tui had at the #2899 split
    (`/test-verdict`, `/issue-label`, `/issue-upsert`, `/purge`), plus #1944's
    resource-shaped successor to `/issue-label`.

    A regression here means an endpoint quietly dropped out of the generated
    client and went back to being a hand-built literal.
    """
    covered = {ep.path for ep in RUST_WRITE_ENDPOINTS}
    assert {
        "/test-verdict",
        "/issue-label",
        "/issue-upsert",
        "/purge",
        "/issue/{repo_name}/{number}",
    } <= covered


def test_generated_output_is_non_empty_rust():
    generated = generate_rust_requests()
    assert generated.startswith("//! AUTO-GENERATED")
    assert generated.endswith("\n")
    assert "serde::Serialize" in generated
    assert "serde::Deserialize" in generated


def test_no_accidental_doctest_in_the_header():
    """rustdoc compiles an unannotated code block in a doc comment as a Rust
    doctest — `cargo test --doc` then fails on a shell command. Same trap
    `generated.rs`'s header documents."""
    for line in generate_rust_requests().splitlines():
        stripped = line.strip()
        if stripped.startswith("//!") and "```" in stripped:
            assert "```text" in stripped or stripped.endswith("```"), (
                f"unannotated doc fence would compile as a doctest: {line!r}"
            )


# ── the drift-detection property coord-tui's CI converts into a red build ────


def test_a_renamed_wire_field_changes_the_generated_output(monkeypatch):
    """The acceptance criterion, proved locally: rename a field on a body
    this client sends and the generated Rust must change.

    coord-tui's CI turns exactly this difference into a red `--rust --check`;
    without it, a rename here would ship a client that writes a key the
    daemon ignores.
    """
    spec = copy.deepcopy(codegen.board_openapi_spec())
    props = spec["paths"]["/test-verdict"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["properties"]
    props["verdict_state"] = props.pop("test_state")
    monkeypatch.setattr(codegen, "board_openapi_spec", lambda: spec)

    generated = generate_rust_requests()

    assert "verdict_state" in generated
    assert "pub(crate) test_state" not in generated


def test_an_endpoint_the_spec_no_longer_declares_is_a_hard_error(monkeypatch):
    """Silently skipping a vanished route would generate a client missing the
    call it was asked for — a `--check` that passes while the contract is
    gone. Raise instead."""
    spec = copy.deepcopy(codegen.board_openapi_spec())
    del spec["paths"]["/purge"]
    monkeypatch.setattr(codegen, "board_openapi_spec", lambda: spec)

    with pytest.raises(WriteEndpointError, match="/purge"):
        generate_rust_requests()


def test_an_endpoint_with_no_json_body_is_a_hard_error(monkeypatch):
    spec = copy.deepcopy(codegen.board_openapi_spec())
    del spec["paths"]["/purge"]["post"]["requestBody"]
    monkeypatch.setattr(codegen, "board_openapi_spec", lambda: spec)

    with pytest.raises(WriteEndpointError, match="requestBody"):
        generate_rust_requests()


def test_a_dangling_ref_is_a_hard_error(monkeypatch):
    spec = copy.deepcopy(codegen.board_openapi_spec())
    spec["paths"]["/issue-upsert"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]["properties"]["issue"] = {"$ref": "#/components/schemas/Vanished"}
    monkeypatch.setattr(codegen, "board_openapi_spec", lambda: spec)

    with pytest.raises(WriteEndpointError, match="Vanished"):
        generate_rust_requests()


# ── absent-vs-null: the semantics JSON Schema cannot express ─────────────────


def _struct_body(generated: str, name: str) -> str:
    start = generated.index(f"struct {name} {{")
    return generated[start : generated.index("\n}", start)]


def test_a_verb_route_sends_an_explicit_null_for_an_omitted_field():
    """`record_test_verdict_remote`'s literal sends `"test_reason": null`
    today. A generated client that started omitting the key instead would be
    a behaviour change smuggled in under "no functional change"."""
    body = _struct_body(generate_rust_requests(), "TestVerdictRequest")
    assert "pub(crate) test_reason: Option<String>," in body
    assert "skip_serializing_if" not in body


def test_a_verb_routes_required_fields_are_not_optional():
    body = _struct_body(generate_rust_requests(), "TestVerdictRequest")
    assert "pub(crate) assignment_id: String," in body
    assert "pub(crate) test_state: String," in body


def test_a_patch_route_distinguishes_absent_from_null():
    """`coord/rest_schema.py`: an explicit null milestone CLEARS it, an
    omitted key leaves it alone. A plain `Option<i64>` can only express two
    of those three states, making "clear the milestone" unreachable from a
    generated client."""
    body = _struct_body(generate_rust_requests(), "IssuePatchRequest")
    assert "pub(crate) milestone: Option<Option<i64>>," in body
    assert body.count('skip_serializing_if = "Option::is_none"') >= 1


def test_every_patch_field_is_skippable():
    """A PATCH that serialized its untouched fields would rewrite the whole
    issue on every call."""
    body = _struct_body(generate_rust_requests(), "IssuePatchRequest")
    field_lines = [line for line in body.splitlines() if line.strip().startswith("pub(crate)")]
    assert len(field_lines) == body.count('skip_serializing_if = "Option::is_none"')


def test_response_fields_all_default():
    """The #632/#546/#628 lesson applied to the write path: one unexpected key
    must not turn a successful write into a displayed parse error."""
    body = _struct_body(generate_rust_requests(), "PurgeResponse")
    field_lines = [line for line in body.splitlines() if line.strip().startswith("pub(crate)")]
    assert field_lines
    assert body.count("#[serde(default)]") == len(field_lines)


def test_a_nested_body_object_becomes_its_own_struct_not_a_json_value():
    """`/issue-upsert`'s `issue` was the one body whose spec said only
    `{"type": "object"}` — which generates a `serde_json::Value`, i.e. the
    hand-built literal this story exists to delete."""
    generated = generate_rust_requests()
    assert "struct IssueUpsertIssue {" in generated
    assert "pub(crate) issue: IssueUpsertIssue," in generated
    assert "pub(crate) issue: serde_json::Value," not in generated


# ── X-Coord-Schema (#1943) ───────────────────────────────────────────────────


def _impl_body(generated: str, name: str) -> str:
    start = generated.index(f"impl {name} {{")
    return generated[start : generated.index("\n}", start)]


def test_resource_shaped_routes_send_the_schema_header():
    from coord.dao import SCHEMA_VERSION

    body = _impl_body(generate_rust_requests(), "IssuePatchRequest")
    assert f"const SCHEMA_HEADER: Option<u32> = Some({SCHEMA_VERSION});" in body


def test_verb_routes_send_no_schema_header():
    """Absence means "today's shape", which is what keeps an un-migrated verb
    call working unchanged (docs/STORE_SERVICE.md §4)."""
    generated = generate_rust_requests()
    for ep in RUST_WRITE_ENDPOINTS:
        if ep.is_resource_shaped:
            continue
        body = _impl_body(generated, f"{ep.base}Request")
        assert "SCHEMA_HEADER: Option<u32> = None;" in body, ep.path


def test_resource_shaped_is_derived_from_the_path_not_declared():
    assert WriteEndpoint(path="/issue/{n}", method="patch", base="X").is_resource_shaped
    assert not WriteEndpoint(path="/issue-label", method="post", base="X").is_resource_shaped


def test_a_parameterised_route_gets_a_path_builder():
    """The last hand-built string in the client: a `format!` of the route."""
    body = _impl_body(generate_rust_requests(), "IssuePatchRequest")
    assert "fn path(repo_name: &str, number: u64) -> String {" in body
    assert 'format!("/issue/{repo_name}/{number}")' in body


def test_a_bare_route_gets_a_borrowing_path_accessor():
    body = _impl_body(generate_rust_requests(), "PurgeRequest")
    assert "fn path() -> &'static str {" in body
    assert 'const PATH: &\'static str = "/purge";' in body


# ── destination resolution ───────────────────────────────────────────────────


def test_requests_out_flag_wins_outright(tmp_path, monkeypatch):
    monkeypatch.setenv(RUST_OUTPUT_ENV_VAR, str(tmp_path / "from-env"))
    explicit = tmp_path / "explicit" / "generated_requests.rs"
    assert resolve_rust_requests_output_path(explicit) == explicit


def test_it_lands_beside_an_explicit_out(tmp_path):
    """`--rust` stays ONE command: `--out` names the read file and the write
    file is its sibling, which is where `$COORD_TUI_SRC` would put it too."""
    board = tmp_path / "types" / "generated.rs"
    assert (
        resolve_rust_requests_output_path(None, board_out=board)
        == tmp_path / "types" / "generated_requests.rs"
    )


def test_env_var_resolves_relative_to_a_checkout_root(tmp_path, monkeypatch):
    monkeypatch.setenv(RUST_OUTPUT_ENV_VAR, str(tmp_path))
    assert (
        resolve_rust_requests_output_path(None) == tmp_path / RUST_REQUESTS_OUTPUT_RELPATH
    )


def test_no_destination_is_an_error_not_a_guess(monkeypatch):
    monkeypatch.delenv(RUST_OUTPUT_ENV_VAR, raising=False)
    with pytest.raises(OutputPathError):
        resolve_rust_requests_output_path(None)


# ── the `--rust` CLI seam covers BOTH files ──────────────────────────────────


def test_rust_writes_both_files(tmp_path, monkeypatch):
    monkeypatch.setenv(RUST_OUTPUT_ENV_VAR, str(tmp_path))

    assert codegen.main(["--rust"]) == 0

    assert (tmp_path / codegen.RUST_OUTPUT_RELPATH).is_file()
    assert (tmp_path / RUST_REQUESTS_OUTPUT_RELPATH).is_file()


def test_rust_check_passes_when_both_are_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv(RUST_OUTPUT_ENV_VAR, str(tmp_path))
    assert codegen.main(["--rust"]) == 0
    assert codegen.main(["--rust", "--check"]) == 0


def test_rust_check_fails_when_only_the_write_client_is_stale(tmp_path, monkeypatch):
    """The regression this whole story guards: the read half is fine, the
    write half has drifted, and one command has to notice."""
    monkeypatch.setenv(RUST_OUTPUT_ENV_VAR, str(tmp_path))
    assert codegen.main(["--rust"]) == 0
    (tmp_path / RUST_REQUESTS_OUTPUT_RELPATH).write_text("// stale\n")

    assert codegen.main(["--rust", "--check"]) == 1


def test_rust_check_fails_when_the_write_client_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv(RUST_OUTPUT_ENV_VAR, str(tmp_path))
    assert codegen.main(["--rust"]) == 0
    (tmp_path / RUST_REQUESTS_OUTPUT_RELPATH).unlink()

    assert codegen.main(["--rust", "--check"]) == 1


def test_rust_check_reports_both_files_before_returning(tmp_path, monkeypatch, capsys):
    """Stopping at the first stale file sends the reader back for a second
    round trip to discover the other."""
    monkeypatch.setenv(RUST_OUTPUT_ENV_VAR, str(tmp_path))
    assert codegen.main(["--rust"]) == 0
    (tmp_path / codegen.RUST_OUTPUT_RELPATH).write_text("// stale\n")
    (tmp_path / RUST_REQUESTS_OUTPUT_RELPATH).write_text("// stale\n")
    capsys.readouterr()

    assert codegen.main(["--rust", "--check"]) == 1

    err = capsys.readouterr().err
    assert "generated.rs is stale" in err
    assert "generated_requests.rs is stale" in err


def test_requests_out_overrides_the_sibling_derivation(tmp_path, monkeypatch):
    monkeypatch.delenv(RUST_OUTPUT_ENV_VAR, raising=False)
    board = tmp_path / "a" / "generated.rs"
    requests = tmp_path / "b" / "requests.rs"

    assert (
        codegen.main(
            ["--rust", "--out", str(board), "--requests-out", str(requests)]
        )
        == 0
    )

    assert board.is_file()
    assert requests.is_file()
    assert not (tmp_path / "a" / "generated_requests.rs").exists()
