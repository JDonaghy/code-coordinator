"""Tests for scripts/comment_cruft.py: the Rust lexer, the mechanical strip,
the comment-only verifier and the model-rewrite apply step (no model calls)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import comment_cruft as cc  # noqa: E402


def kinds(src: str) -> list[tuple[str, str]]:
    return [(s.kind, src[s.start : s.end]) for s in cc.lex(src) if s.kind != "code"]


# --- lexer -----------------------------------------------------------------


def test_lex_does_not_treat_slashes_in_strings_as_comments():
    src = 'let u = "http://x"; // real\n'
    assert kinds(src) == [("str", '"http://x"'), ("line", "// real")]


def test_lex_raw_and_byte_strings():
    src = 'let a = r#"a // "b" "#; let b = br"//"; let c = b"//"; // c\n'
    assert [k for k, _ in kinds(src)] == ["str", "str", "str", "line"]


def test_lex_char_literal_vs_lifetime():
    src = "fn f<'a>(x: &'a str) -> char { if x == \"\" { '/' } else { '\\'' } } // c\n"
    found = kinds(src)
    assert ("str", "'/'") in found and ("str", "'\\''") in found
    assert found[-1] == ("line", "// c")


def test_lex_doc_and_nested_block_comments():
    src = "/// doc\n//! inner\n//// plain\n/* a /* b */ c */ x\n"
    assert kinds(src) == [
        ("doc", "/// doc"),
        ("doc", "//! inner"),
        ("line", "//// plain"),
        ("block", "/* a /* b */ c */"),
    ]


def test_raw_identifier_is_code():
    assert kinds("let r#type = 1; // c\n") == [("line", "// c")]


# --- flagging --------------------------------------------------------------


@pytest.mark.parametrize(
    "body,flagged",
    [
        (" #1630 review: clicking away", True),
        (" see quadraui#12", True),
        (" this used to panic", True),
        (" TODO(#123): drop once upstream ships", False),
        (" colour #282828 is the bg", False),
        (" &#123; entity", False),
        (" plain present-tense comment", False),
    ],
)
def test_is_flagged(body, flagged):
    assert cc.is_flagged(body) is flagged


# --- strip -----------------------------------------------------------------


@pytest.mark.parametrize(
    "body,expected",
    [
        (" #1360: a built-in panel's icon", " A built-in panel's icon"),
        (" #1630 review: clicking away", " Clicking away"),
        (" #940/quadraui#947: opt into CSD", " Opt into CSD"),
        (" handles both cases (#1005).", " handles both cases."),
        (" rather than a handle (#1234, #1529).", " rather than a handle."),
        (" see #883", None),
        (" #1243", None),
        (" TODO(#9): remove", " TODO(#9): remove"),
        (" (#807, `to:yi( cursor` and five siblings)", " (#807, `to:yi( cursor` and five siblings)"),
        (" ── Section title (#1631) ──────", " ── Section title ──────────────"),
    ],
)
def test_strip_body(body, expected):
    assert cc.strip_body(body) == expected


def test_strip_file_deletes_ref_only_lines_and_keeps_code():
    src = (
        "fn f() {\n"
        "    // #12: compute the thing\n"
        "    // see #34\n"
        "    let x = 1; // cache it (#56)\n"
        "    let y = 2; // #78\n"
        "}\n"
    )
    out, n = cc.strip_file(src)
    assert n == 4
    assert out == (
        "fn f() {\n"
        "    // Compute the thing\n"
        "    let x = 1; // cache it\n"
        "    let y = 2;\n"
        "}\n"
    )
    assert cc.normalize(out) == cc.normalize(src)


def test_strip_never_touches_doc_comments():
    src = "/// see #12\nfn f() {}\n"
    assert cc.strip_file(src) == (src, 0)


def test_deleting_a_block_tidies_blank_lines():
    src = "fn f() {\n    // #12\n\n    a();\n\n    // #13\n\n    b();\n}\n"
    out, _ = cc.strip_file(src)
    assert out == "fn f() {\n    a();\n\n    b();\n}\n"


# --- normalize / verify ----------------------------------------------------


def test_normalize_ignores_plain_comments_and_whitespace_only():
    a = "let x = 1; // one\nlet y = 2;\n"
    assert cc.normalize(a) == cc.normalize("let x = 1;\n\n\n// other\nlet y   = 2;")
    assert cc.normalize(a) != cc.normalize("let x = 1; let y = 3;")
    assert cc.normalize('let s = "a  b";') != cc.normalize('let s = "a b";')
    assert cc.normalize("/// doc\nfn f(){}") != cc.normalize("/// changed\nfn f(){}")
    assert cc.normalize("a //c\n/b") != cc.normalize("a //c\n /b".replace("\n /", "\n/").replace("//c\n", "/"))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True, check=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.rs").write_text("fn a() {\n    // #12: do it\n    run();\n}\n")
    (tmp_path / "tests" / "acceptance").mkdir(parents=True)
    (tmp_path / "tests" / "acceptance" / "s.rs").write_text("// #99: sealed\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_strip_then_verify_passes_and_skips_sealed_paths(repo: Path, capsys):
    assert cc.main(["strip", str(repo)]) == 0
    assert (repo / "src" / "a.rs").read_text() == "fn a() {\n    // Do it\n    run();\n}\n"
    assert (repo / "tests" / "acceptance" / "s.rs").read_text() == "// #99: sealed\n"
    assert cc.main(["verify", str(repo), "--base", "HEAD"]) == 0


def test_verify_fails_on_a_code_change(repo: Path, capsys):
    (repo / "src" / "a.rs").write_text("fn a() {\n    run2();\n}\n")
    assert cc.main(["verify", str(repo), "--base", "HEAD"]) == 1
    assert "non-comment tokens changed" in capsys.readouterr().out


def test_verify_fails_on_non_rust_change_unless_allowed(repo: Path):
    (repo / "x.json").write_text("{}")
    _git(repo, "add", "x.json")
    assert cc.main(["verify", str(repo), "--base", "HEAD"]) == 1
    assert cc.main(["verify", str(repo), "--base", "HEAD", "--allow", "*.json"]) == 0


# --- rewrite apply ---------------------------------------------------------

SRC = (
    "fn f() {\n"
    "    // #12 review: the cache used to be\n"
    "    // rebuilt here; now it is reused.\n"
    "    let c = cache(); // used to clone\n"
    "    // fine comment\n"
    "    // pre-#40 path\n"
    "    go(c);\n"
    "}\n"
)


def fake(blocks: list[dict]):
    def answer(prompt: str):
        assert "### block 0" in prompt and "TRAILING" in prompt
        return "```json\n" + json.dumps({"blocks": blocks}) + "\n```", 0.001

    return answer


def test_rewrite_applies_replace_delete_and_trailing():
    res = cc.rewrite_source(
        SRC,
        fake(
            [
                {"id": 0, "action": "replace", "lines": ["// The cache is reused, not rebuilt."]},
                {"id": 1, "action": "delete"},
                {"id": 2, "action": "replace", "lines": ["// fine comment"]},
            ]
        ),
    )
    assert res.blocks == 3 and res.changed == 3 and not res.rejected
    assert res.new_src == (
        "fn f() {\n"
        "    // The cache is reused, not rebuilt.\n"
        "    let c = cache();\n"
        "    // fine comment\n"
        "    go(c);\n"
        "}\n"
    )


@pytest.mark.parametrize(
    "answer",
    [
        {"id": 0, "action": "replace", "lines": ["/// doc now"]},
        {"id": 0, "action": "replace", "lines": ["let x = 1;"]},
        {"id": 1, "action": "replace", "lines": ["// a", "// b"]},
        {"id": 0, "action": "replace", "lines": ["// " + "x" * 120]},
        {"id": 7, "action": "delete"},
    ],
)
def test_rewrite_rejects_invalid_answers(answer):
    res = cc.rewrite_source(SRC, fake([answer]))
    assert res.rejected and res.new_src is None


# --- doc comments (--docs) -------------------------------------------------

DOC_SRC = (
    "/// Draws the frame (#12).\n"
    "///\n"
    "/// see #34\n"
    "///\n"
    "/// ```\n"
    "/// let x = 1; // (#56)\n"
    "/// ```\n"
    "pub fn draw() {}\n"
)


def test_doc_comments_untouched_without_docs_flag():
    assert cc.strip_file(DOC_SRC) == (DOC_SRC, 0)


def test_doc_strip_skips_fences_and_tidies_blank_doc_lines():
    out, _ = cc.strip_file(DOC_SRC, docs=True)
    assert out == (
        "/// Draws the frame.\n"
        "///\n"
        "/// ```\n"
        "/// let x = 1; // (#56)\n"
        "/// ```\n"
        "pub fn draw() {}\n"
    )
    assert cc.normalize(out, docs=True) == cc.normalize(DOC_SRC, docs=True)


def test_doc_block_is_never_emptied():
    src = "//! #12\nfn f() {}\n"
    assert cc.strip_file(src, docs=True) == (src, 0)


def test_verify_docs_still_pins_doctest_code():
    changed = DOC_SRC.replace("let x = 1;", "let x = 2;")
    assert cc.normalize(changed, docs=True) != cc.normalize(DOC_SRC, docs=True)
    prose = DOC_SRC.replace("Draws the frame", "Paints the frame")
    assert cc.normalize(prose, docs=True) == cc.normalize(DOC_SRC, docs=True)
    assert cc.normalize(prose) != cc.normalize(DOC_SRC)


def test_doc_rewrite_requires_the_blocks_own_prefix():
    src = "/// Frame drawer; used to flicker.\npub fn draw() {}\n"

    def answer(lines):
        return lambda prompt: (json.dumps({"blocks": [{"id": 0, "action": "replace", "lines": lines}]}), 0.0)

    assert "DOC(///)" in cc.build_prompt(src, [b for b in cc.blocks(cc.line_comments(src, True)) if b.flagged])
    bad = cc.rewrite_source(src, answer(["// Frame drawer."]), docs=True)
    assert bad.rejected and bad.new_src is None
    good = cc.rewrite_source(src, answer(["/// Frame drawer."]), docs=True)
    assert good.new_src == "/// Frame drawer.\npub fn draw() {}\n"


def test_rewrite_retries_one_malformed_reply():
    replies = iter(["{not json", json.dumps({"blocks": [{"id": 0, "action": "delete"}]})])
    res = cc.rewrite_source("fn f() {\n    // #12 thing\n    g();\n}\n", lambda p: (next(replies), 0.0))
    assert res.new_src == "fn f() {\n    g();\n}\n"


def test_doc_replacement_may_not_carry_a_fence_line():
    assert not cc.valid_line("//! ```", "//!")
    assert not cc.valid_line("/// ~~~rust", "///")
    assert cc.valid_line("/// Uses `x`.", "///")


def test_deleting_the_last_comment_in_a_block_drops_the_blank_before_the_brace():
    src = "fn f() {\n    a();\n\n    // #12\n}\n"
    assert cc.strip_file(src)[0] == "fn f() {\n    a();\n}\n"


def test_normalize_ignores_layout_but_not_operator_splits():
    assert cc.normalize("const K: &[&str] = &[\n];") == cc.normalize("const K: &[&str] = &[];")
    assert cc.normalize("a >> b") != cc.normalize("a > > b")
    assert cc.normalize("ab") != cc.normalize("a b")


def test_rewrite_strips_indentation_the_model_adds():
    src = "fn f() {\n    // #12 thing\n    g();\n}\n"
    reply = json.dumps({"blocks": [{"id": 0, "action": "replace", "lines": ["    // Thing."]}]})
    assert cc.rewrite_source(src, lambda p: (reply, 0.0)).new_src == "fn f() {\n    // Thing.\n    g();\n}\n"
