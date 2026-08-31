"""#2533 (ms-67 contract §4c): dispatch a `type="decomposition-chat"` session
for "Pull into decomposition session" — the TUI action that turns an
Approved-work-items row that the client has actually signed off on into a
briefed `claude -p` chat whose job is to decide whether the work is
oracle-loop-shaped, file the resulting GitHub issue(s) via `coord issue
create`, and queue them via `coord drive-queue add`.

Used by `coord portal decompose-chat <submission_id>` (the CLI command the
TUI shells out to, mirroring `coord new-issue-chat` / `coord milestone chat`
— see `coord/new_issue_chat.py` / `coord/milestone_dispatch.py` for the two
precedents this module follows).

**Reads through the existing bridge, never a new direct portal read**
(#2533's own briefing): the submission's four briefing fields (outcome /
audience / done-definition / constraints) plus its server-resolved mapped
repo(s) come from :func:`coord.approved_work.approved_submissions` — the same
function that already serves the TUI's `/board` `approved_submissions` block
(#2532) — rather than re-deriving them from `coord.portal_store` directly.

**#2661:** that shared function now also returns never-signed-off
`signoff_status == "new"` rows (a request that has arrived but that nobody
has acted on yet), so :func:`dispatch_decomposition_chat` filters on
`signoff_status == "approved"` itself before treating a row as eligible —
see that function's docstring. Filing real issues and queuing real dispatch
work must stay gated on an actual customer sign-off; the panel's own
"nothing started yet" widening must not silently become a second way to
skip that gate.

**Machine selection is generalized to every mapped repo** (contract §4c):
unlike :func:`coord.new_issue_chat.pick_new_issue_chat_machine` (one repo),
a submission can map to more than one repo, and the picked machine must be
able to work on ALL of them — a session that can only reach some of the
repos it's meant to decompose into is worse than refusing outright. §6.7
flags this as a genuinely open question the contract does not resolve beyond
"refuse clearly" — this module's refusal is a plain `RuntimeError`, same
posture every other dispatcher in this file already uses.
"""
from __future__ import annotations

import datetime
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from coord.config import Config
    from coord.models import Machine

_log = logging.getLogger(__name__)

#: Sentinel `issue_title` this module writes onto the dispatched assignment
#: — mirrors `dispatch_new_issue_chat`'s own `"(new issue draft)"` sentinel
#: for a chat type with no real GitHub issue yet. The TUI's own bind path
#: (`tui/src/app/dialogs.rs::maybe_bind_pending_decomposition_chat`) matches
#: on this exact string (with the submission id substituted in) since
#: `Assignment` carries no `submission_id` column of its own — see that
#: function's doc comment for why this is the module's own choice, not
#: something #2533's contract pins.
def _issue_title(submission_id: str) -> str:
    return f"decomposition: {submission_id}"


# ── #2750 (IL-4): mode selection — ask/propose posture vs file-straight-through ──

#: The portal's own placeholder for a field the client's quick-submission
#: flow never asked for (see #2750's issue body, `portal_submissions.
#: customer_json`) — coord doesn't own this string, it just recognizes it as
#: equivalent to "missing" the same way an empty string is.
NOT_CAPTURED_SENTINEL = "Not captured at first contact"


def _field_missing(value: Any) -> bool:
    """True when *value* carries no real content — absent, blank, or the
    portal's own "not captured at first contact" placeholder (#2750).

    #2864: the portal never sends the bare sentinel — it's the leading
    clause of a longer sentence (e.g. ``"Not captured at first contact —
    this came in through the contact form, so it still needs to be agreed
    with the customer."``, em dash and all), so this matches the sentinel
    as a **prefix**, casefolded and whitespace-normalised, rather than by
    exact equality. A prefix match still leaves real content that merely
    *mentions* the phrase mid-sentence reading as present, since the
    sentinel then isn't at the start.
    """
    if not isinstance(value, str):
        return True
    text = " ".join(value.split())
    if not text:
        return True
    sentinel = " ".join(NOT_CAPTURED_SENTINEL.split())
    return text.casefold().startswith(sentinel.casefold())


#: Files `coord repo create` seeds into a brand-new repo as its first
#: commit(s) (#2747): `README.md` from `create_repo`'s own `--add-readme`,
#: plus `CLAUDE.md`, a CI workflow, and the ported `.githooks/*` from its
#: own seed commit (`coord.commands.repo._seed_files`). Kept here rather
#: than imported from `coord.commands.repo` — this module's briefing scopes
#: it to `coord/decomposition_chat.py` alone, and the set is small and
#: unlikely to drift silently (a drift only ever costs one extra spurious
#: DISCUSS round, never a false FILE).
_SEEDED_ROOT_FILES = frozenset({"README.md", "CLAUDE.md"})
_SEEDED_ROOT_DIRS = frozenset({".github", ".githooks"})
_SEEDED_GITHUB_WORKFLOW_FILES = frozenset({"ci.yml"})
_SEEDED_GITHOOKS_FILES = frozenset(
    {"_lib.sh", "post-checkout", "post-commit", "post-merge"}
)


def _dir_entries(repo: str, path: str, branch: str) -> tuple[list[str], list[str]]:
    """``(files, dirs)`` directly under *path* on *branch* — ``([], [])``
    when *path* doesn't exist there at all (e.g. a repo that never got
    `.githooks/` seeded) rather than raising, mirroring `github_ops.
    repo_file_exists`'s own not-found handling. Any failure that isn't a
    clean 404 propagates, same reasoning: a caller here must not read
    "couldn't check" as "empty directory".
    """
    from coord import github_ops  # noqa: PLC0415

    try:
        files = github_ops.list_repo_dir(repo, path, branch)
        dirs = github_ops.list_repo_subdirs(repo, path, branch)
    except RuntimeError as exc:
        msg = str(exc).lower()
        if "not found" in msg or "404" in msg:
            return [], []
        raise
    return files, dirs


def _repo_has_only_seeded_files(repo: str, branch: str) -> bool:
    """True when every tracked file on *branch* of *repo* is one that
    `coord repo create` itself seeds (#2864 bug 2) — walks the small, fixed
    set of directories seeding can ever touch (root, `.github/`,
    `.github/workflows/`, `.githooks/`); any file or directory outside that
    set is real product history.
    """
    files, dirs = _dir_entries(repo, "", branch)
    if not set(files) <= _SEEDED_ROOT_FILES:
        return False
    if not set(dirs) <= _SEEDED_ROOT_DIRS:
        return False

    if ".github" in dirs:
        gh_files, gh_dirs = _dir_entries(repo, ".github", branch)
        if gh_files or set(gh_dirs) - {"workflows"}:
            return False
        if "workflows" in gh_dirs:
            wf_files, wf_dirs = _dir_entries(repo, ".github/workflows", branch)
            if not set(wf_files) <= _SEEDED_GITHUB_WORKFLOW_FILES or wf_dirs:
                return False

    if ".githooks" in dirs:
        hook_files, hook_dirs = _dir_entries(repo, ".githooks", branch)
        if not set(hook_files) <= _SEEDED_GITHOOKS_FILES or hook_dirs:
            return False

    return True


def _repo_is_greenfield(cfg: "Config", repo_name: str) -> bool:
    """Mechanical "nothing to decompose against yet" signal for *repo_name*
    (#2750's second mode-selection trigger): no commits on its default
    branch, or every tracked file there is one `coord repo create` itself
    seeds (:func:`_repo_has_only_seeded_files`).

    #2864: used to be "no `CLAUDE.md` there", which `coord repo create`
    (#2747) broke — it seeds `CLAUDE.md` as part of a brand-new repo's
    first commit, so a repo created through the intake lane's own genesis
    command could never be detected as greenfield by the intake lane's own
    mode selector.

    Fails safe toward ``True`` (i.e. toward MODE: DISCUSS) on an unmapped
    repo or any lookup failure — a false positive here costs one extra
    discuss round; a false negative means filing straight through against a
    repo with no history and no rules to decompose against, which is
    exactly the failure #2750 exists to prevent.
    """
    from coord import github_ops  # noqa: PLC0415

    repo_cfg = cfg.repo(repo_name)
    if repo_cfg is None:
        return True
    branch = repo_cfg.default_branch or "main"
    # get_branch_sha is itself fail-safe (returns None on any lookup
    # failure, transient or not) — that None is indistinguishable here from
    # "genuinely no commits yet", which is the conservative reading anyway.
    sha = github_ops.get_branch_sha(repo_cfg.github, branch)
    if sha is None:
        return True
    try:
        return _repo_has_only_seeded_files(repo_cfg.github, branch)
    except RuntimeError:
        return True


def select_discuss_mode(
    cfg: "Config",
    submission: dict[str, Any],
    *,
    discuss_override: bool | None = None,
) -> tuple[bool, str]:
    """Pick MODE: DISCUSS (ask/propose/decompose loop) vs MODE: FILE
    (decompose straight through, #2533's original single posture) for
    *submission* — #2750's "Mode selection" section.

    *discuss_override* is ``--discuss``/``--no-discuss`` from the CLI: when
    not ``None`` it wins outright over the mechanical triggers below. The
    two triggers are deliberately mechanical, no judgment call:

    * `done_definition` / `audience` missing, blank, or the portal's own
      "not captured at first contact" sentinel;
    * any mapped repo with no commits on its default branch, or whose
      tracked files are only what `coord repo create` itself seeds
      (:func:`_repo_is_greenfield`).

    Returns ``(discuss, reason)`` — *reason* is always non-empty and is
    meant to be the first thing the operator reads (#2750: "a session that
    silently chose to file is the failure being fixed").
    """
    if discuss_override is not None:
        return (
            discuss_override,
            "--discuss forced it on" if discuss_override else "--no-discuss forced it off",
        )

    reasons: list[str] = []
    if _field_missing(submission.get("done_definition")):
        reasons.append("done_definition is missing/blank/not captured")
    if _field_missing(submission.get("audience")):
        reasons.append("audience is missing/blank/not captured")

    repos: list[str] = submission.get("repos") or []
    greenfield = [r for r in repos if _repo_is_greenfield(cfg, r)]
    if greenfield:
        reasons.append(
            f"mapped repo(s) {', '.join(greenfield)} have no commits or only "
            "coord repo create's seed files — nothing to decompose against yet"
        )

    if reasons:
        return True, "under-specified/greenfield: " + "; ".join(reasons)
    return (
        False,
        "done_definition and audience are captured and every mapped repo "
        "has history to decompose against",
    )


def fetch_running_context(submission_id: str) -> dict[str, Any]:
    """*submission_id*'s running-context ledger payload (#2749's four-layer
    store), routed through the daemon when this machine is a thin client —
    the same #2751 exception `coord portal ledger` already gets, so a
    briefing built on ANY machine sees the exact same context a daemon-host
    invocation would (#2750's own "Done when": a different session, on a
    different machine, briefed with everything so far).

    The daemon GET itself is :func:`coord.client.fetch_portal_ledger` —
    shared with `coord portal ledger`'s own remote read (#2750 fix round:
    the two used to carry independent near-verbatim copies of the same
    GET/params/header/wire-shape logic; factored into `coord.client`'s
    `fetch_*` family, which both callers already reach into for other
    daemon calls, so the two can't drift on the wire shape again).
    """
    from coord import board_service  # noqa: PLC0415

    svc = board_service.resolve()
    if svc is not None:
        from coord.client import fetch_portal_ledger  # noqa: PLC0415

        return fetch_portal_ledger(svc, submission_id)
    from coord import portal_store  # noqa: PLC0415

    return portal_store.render_ledger_payload(submission_id)


def _render_answer_line(a: dict[str, Any]) -> str:
    """One rendered answer line — mirrors :func:`coord.commands.portal.
    _render_answer_line` exactly (kept as a separate copy since the two
    modules render different surrounding structure — a plain-text agent
    briefing here, ``coord portal ledger``'s CLI output there).

    #2986: a relayed (out-of-band) answer must never read as something the
    client typed themselves — a session briefed from this text has no other
    way to tell the difference, so the RELAYED tag, its source, and when it
    was recorded are load-bearing here, not cosmetic. ``.get`` throughout
    because a thin client's daemon may predate #2986 and never sets either
    key."""
    if not a.get("relayed"):
        return f"A: {a.get('text', '')}  (by {a.get('actor') or 'customer'})"
    recorded_at = a.get("recorded_at")
    when = (
        datetime.datetime.fromtimestamp(recorded_at, tz=datetime.timezone.utc)
        .strftime("%Y-%m-%d %H:%M UTC")
        if recorded_at
        else "unknown date"
    )
    source = a.get("source") or "unknown"
    return (
        f"A [RELAYED via {source}, {when}, by {a.get('actor') or 'operator'}]: "
        f"{a.get('text', '')}"
    )


def render_running_context_section(payload: dict[str, Any]) -> str:
    """Render *payload* (:func:`fetch_running_context`'s shape) as the
    briefing's RUNNING CONTEXT section — every question asked and its
    answer (if any), every decision on record (current + archived, archived
    ones carrying why), and the current narrative. This is what lets a
    fresh iteration pick up exactly where the last one left off with no
    memory of the prior session (#2750's "the loop").

    Since #2867 it also renders the ledger's operator-note layer — what a
    human relayed out of band ("I spoke to her; it's just the two of them")
    — attributed as operator-supplied so a session can tell it from
    something the client wrote on the portal itself.
    """
    lines: list[str] = ["RUNNING CONTEXT (from the portal ledger):", "", "Q&A so far:"]
    qa = payload.get("qa") or []
    unpaired = payload.get("unpaired_answers") or []
    if not qa and not unpaired:
        lines.append("  (none yet)")
    for entry in qa:
        rev = entry.get("question_revision")
        lines.append(f"  - Q[{rev if rev is not None else '?'}] {entry.get('question', '')}")
        answers = entry.get("answers") or []
        if answers:
            for a in answers:
                lines.append(f"      {_render_answer_line(a)}")
        else:
            lines.append("      (unanswered — needs-input)")
    for a in unpaired:
        tag = " RELAYED" if a.get("relayed") else ""
        lines.append(
            f"  - A{tag} (unpaired, question_revision={a.get('question_revision')}): "
            f"{a.get('text', '')}  (by {a.get('actor') or 'customer'})"
        )

    # #2867: verbatim and attributed, so a session on a different machine
    # with no shared transcript still sees what the operator was told out of
    # band. Placed ahead of decisions because it is INPUT to them.
    notes = payload.get("operator_notes") or []
    if notes:
        lines.append("")
        lines.append(
            "Operator-supplied background (relayed by a human, NOT written by "
            "the client on the portal — treat as fact, do not re-ask it):"
        )
        for n in notes:
            lines.append(
                f"  - [{n.get('seq')}] {n.get('text')}  "
                f"(by {n.get('actor') or 'operator'})"
            )

    lines.append("")
    lines.append("Current decisions (proposed/confirmed — treat as live guidance):")
    current = payload.get("decisions") or []
    if not current:
        lines.append("  (none yet)")
    for d in current:
        who = f"  (by {d.get('actor')})" if d.get("actor") else ""
        lines.append(f"  - [{d.get('seq')}] {d.get('text')}  [{d.get('state')}]{who}")

    lines.append("")
    lines.append(
        "Archived decisions (superseded/rejected — do NOT re-propose without "
        "new information):"
    )
    archived = payload.get("archived_decisions") or []
    if not archived:
        lines.append("  (none)")
    for d in archived:
        if d.get("state") == "rejected":
            lines.append(f"  - [{d.get('seq')}] {d.get('text')}  REJECTED: {d.get('reason')}")
        else:
            lines.append(
                f"  - [{d.get('seq')}] {d.get('text')}  superseded by "
                f"#{d.get('superseded_by_seq')}"
            )

    narrative = (payload.get("narrative") or "").strip()
    if narrative:
        lines += ["", "Narrative:", narrative]

    return "\n".join(lines)


def pick_decomposition_chat_machine(cfg: "Config", repos: list[str]) -> "Machine | None":
    """Pick a machine that can work on EVERY repo in *repos*.

    Contract §4c: "the picked machine must list **all** of the submission's
    mapped repos ... not just one." Returns the first unpaused machine
    satisfying `m.can_work_on(r)` for every `r` in *repos*. `None` when
    *repos* is empty (nothing to route) or no single machine covers them
    all — callers must treat `None` as a hard refusal, never fall back to a
    partial match.
    """
    from coord.machine_pause import paused_set

    if not repos:
        return None
    paused = paused_set(cfg.machines)
    for m in cfg.machines:
        if m.name in paused:
            continue
        if all(m.can_work_on(r) for r in repos):
            return m
    return None


# ── #2997: HOUSE STACK — the fleet's existing stack, not just this
# submission's mapped repo(s). See the module briefing (SUB-1EA1D3, the
# grocery-list submission) for why this section exists: a MODE: DISCUSS
# iteration proposed Vite+React+Supabase for a greenfield repo without ever
# weighing Cloudflare — the stack every other repo in this org already runs
# and pays for — because nothing in the briefing carried it. This section is
# CONTEXT, not a mandate (the design sketch's own framing): a session may
# still propose something else, but the system prompt (coord/agent.py) is
# what requires it to then record the house alternative as a
# considered-and-rejected decision rather than staying silent about it.

#: Root-level marker file -> the stack signal its mere presence implies.
#: Deliberately mechanical and small, same posture as `_SEEDED_ROOT_FILES`
#: above: cheap to check, cheap to extend, wrong at worst by omission (a
#: repo using something unlisted here just contributes nothing) never by a
#: false positive.
_ROOT_STACK_MARKERS: dict[str, str] = {
    "wrangler.toml": "Cloudflare Workers/Pages (wrangler.toml)",
    "Cargo.toml": "Rust (Cargo.toml)",
    "package.json": "Node/TypeScript (package.json)",
    "pyproject.toml": "Python (pyproject.toml)",
}

#: Same idea, scoped to `.github/workflows/` — a deploy LANE is a workflow
#: file, not a root marker.
_WORKFLOW_STACK_MARKERS: dict[str, str] = {
    "deploy-cloudflare.yml": "Cloudflare Pages deploy (.github/workflows/deploy-cloudflare.yml)",
}

#: `wrangler.toml` binding keys -> the managed Cloudflare service they name
#: (Cloudflare's own `wrangler.toml` schema) — read only when the file is
#: actually present, so this never guesses at a service that isn't wired up.
_WRANGLER_BINDING_MARKERS: dict[str, str] = {
    "d1_databases": "Cloudflare D1 (bound in wrangler.toml)",
    "r2_buckets": "Cloudflare R2 (bound in wrangler.toml)",
    "kv_namespaces": "Cloudflare KV (bound in wrangler.toml)",
}

#: Keywords worth a mention when they show up in a repo's own `CLAUDE.md` —
#: the design sketch's "cheapest read from each repo's own CLAUDE.md" half,
#: for a lane that leaves no marker file at all (e.g. Cloudflare Access,
#: which is edge config, never a repo artifact — coord-portal's own
#: `docs/CUSTOMER_PORTAL.md` names it in prose, not in a file this module
#: could otherwise detect). Casefolded substring match: a false positive
#: here only adds one extra weighed line, never a wrong guess about what
#: exists, since the word is genuinely present in the repo's own docs.
_CLAUDE_MD_KEYWORD_MARKERS: dict[str, str] = {
    "cloudflare access": "Cloudflare Access (mentioned in CLAUDE.md)",
    "cloudflare pages": "Cloudflare Pages (mentioned in CLAUDE.md)",
    "cloudflare d1": "Cloudflare D1 (mentioned in CLAUDE.md)",
    "cloudflare r2": "Cloudflare R2 (mentioned in CLAUDE.md)",
}


def _repo_stack_signals(repo_github: str, branch: str) -> list[str]:
    """Mechanical, best-effort stack signals for *repo_github* on *branch* —
    root-level marker files, known deploy-workflow files under
    `.github/workflows/`, and (when `wrangler.toml` is present) the managed
    Cloudflare services it binds.

    **Degrades gracefully** (#2997 acceptance criterion): any lookup
    failure, or a repo with nothing recognisable, returns `[]` rather than a
    guess — this feeds an informational briefing section, not a gate, so
    silence is always the safe failure mode.

    "Any lookup failure" is meant literally, hence the deliberately broad
    ``except Exception`` guards below rather than the narrow
    ``RuntimeError``/``ValueError`` pair an earlier revision used. The
    Contents-API seam this walks is not exception-typed end to end:
    ``github_ops.list_repo_dir`` indexes ``entry["name"]`` on whatever JSON
    came back (``KeyError``/``TypeError`` on a malformed or
    unexpectedly-shaped payload), and ``_gh`` itself is a subprocess
    boundary that a caller lower in the stack can surface as ``OSError``.
    None of those are worth turning an *informational* paragraph into a
    hard failure of ``coord portal decompose-chat`` for — a crash here
    costs the operator the whole intake session, while a swallowed lookup
    costs one unweighed line.
    """
    from coord import github_ops  # noqa: PLC0415

    try:
        root_files, root_dirs = _dir_entries(repo_github, "", branch)
    except Exception:  # noqa: BLE001 - informational section, never a gate
        return []

    signals = [label for marker, label in _ROOT_STACK_MARKERS.items() if marker in root_files]

    def _file_text(path: str) -> str:
        """*path*'s contents on *branch*, or ``""`` on any lookup failure."""
        try:
            text = github_ops.get_repo_file(repo_github, path, branch)
            # `isinstance` rather than a bare return: a Contents-API payload
            # that decodes to something other than text must not turn a
            # substring probe below into a `TypeError`.
            return text if isinstance(text, str) else ""
        except Exception:  # noqa: BLE001 - see the docstring
            # Covers `binascii.Error` (a ValueError subclass) from
            # `get_repo_file_with_sha`'s `base64.b64decode` on a malformed
            # Contents-API response, `GhError` from the subprocess boundary,
            # and anything else the seam can surface: this feeds an
            # informational section, not a gate, so a failed read degrades
            # to "no signal" rather than blowing up the briefing build.
            return ""

    def _entries(path: str) -> tuple[list[str], list[str]]:
        """``(files, dirs)`` under *path*, or ``([], [])`` on any failure."""
        try:
            return _dir_entries(repo_github, path, branch)
        except Exception:  # noqa: BLE001 - see the docstring
            return [], []

    if "wrangler.toml" in root_files:
        wrangler_text = _file_text("wrangler.toml")
        signals += [
            label for key, label in _WRANGLER_BINDING_MARKERS.items() if key in wrangler_text
        ]

    if "CLAUDE.md" in root_files:
        claude_md_text = _file_text("CLAUDE.md").casefold()
        signals += [
            label for key, label in _CLAUDE_MD_KEYWORD_MARKERS.items() if key in claude_md_text
        ]

    if ".github" in root_dirs:
        _, gh_dirs = _entries(".github")
        if "workflows" in gh_dirs:
            wf_files, _ = _entries(".github/workflows")
            signals += [
                label for marker, label in _WORKFLOW_STACK_MARKERS.items() if marker in wf_files
            ]

    return signals


def house_stack_context(cfg: "Config", exclude_repos: list[str] | None = None) -> str:
    """Render the HOUSE STACK briefing section: what the *rest* of the
    fleet's repos already run and deploy on, derived mechanically from each
    registered repo's tracked files rather than hand-maintained (the design
    sketch's own preference — a hand-maintained approved-stack list rots and
    suppresses the reasoning the decision archive exists to capture).

    *exclude_repos* is normally the submission's own mapped repo(s) — the
    one(s) actually being decomposed, which say nothing about "the REST of
    the fleet" even when they aren't greenfield.

    A repo that contributes no recognisable signal (no CLAUDE.md, no known
    marker file, no known workflow file) is omitted from the per-repo list
    entirely — #2997's "degrades gracefully" criterion: nothing beats a
    wrong guess. When *no* repo in the fleet contributes anything, the whole
    section reads as empty rather than asserting a managed-services list or
    a host-coupled gate that nothing here actually evidences.

    One sick repo never costs the operator the whole intake session: each
    repo's probe is isolated, so a lookup that blows up for `acme/api`
    still leaves `acme/coord-portal`'s Cloudflare signal in the rendered
    section. This section is built on the critical path of *every*
    `coord portal decompose-chat` (headless and `--interactive` alike) —
    an informational paragraph must never be able to abort a dispatch.
    """
    exclude = set(exclude_repos or [])
    per_repo: list[str] = []
    saw_cloudflare = False
    for repo_cfg in cfg.repos:
        if repo_cfg.name in exclude:
            continue
        branch = repo_cfg.default_branch or "main"
        try:
            signals = _repo_stack_signals(repo_cfg.github, branch)
        except Exception:  # noqa: BLE001 - one repo must not sink the section
            continue
        if not signals:
            continue
        per_repo.append(f"- {repo_cfg.name} ({repo_cfg.github}): {'; '.join(signals)}")
        if any("Cloudflare" in s for s in signals):
            saw_cloudflare = True

    if not per_repo:
        return (
            "HOUSE STACK (fleet-wide, #2997): no recognisable stack/deploy signal on any "
            "other registered repo — nothing to weigh here."
        )

    lines = [
        "HOUSE STACK (fleet-wide, not just this submission's mapped repo(s) — #2997):",
        *per_repo,
        "",
        "This is CONTEXT, not a mandate: propose something else if it genuinely fits "
        "better, but if you do, record the house-stack alternative as a "
        "considered-and-rejected decision with a reason (`coord portal decision propose` "
        "then `coord portal decision reject ... \"<why the house stack loses>\"`) instead "
        "of leaving it unweighed.",
    ]
    if saw_cloudflare:
        lines.append("")
        lines.append(
            "Gate that assumes a host: `coord portal enqueue-preview` reads a Cloudflare "
            "Pages PREVIEW deployment URL created per-PR by `cloudflare/pages-action` "
            "(docs/CUSTOMER_PORTAL.md) — choosing a different host means the #2359 "
            "customer preview-approval gate does not apply without new coord work."
        )
    return "\n".join(lines)


def repo_topology_context(cfg: "Config", repos: list[str]) -> str:
    """One paragraph of `coordinator.yml` topology per mapped repo — the
    "coordinator.yml topology context ... the same way docs/CUSTOMER_PORTAL.md's
    design-round step already uses it" #2533's own body asks for.

    No design-round dispatcher exists yet to copy a wire shape from (flagged
    in ms-67 contract.md §5's own notes as a proposal, not a read-off-the-
    schema fact) — this is this module's own reasonable rendering: the
    repo's GitHub slug, its `depends_on`, and which configured machines
    claim it.
    """
    lines: list[str] = []
    for repo_name in repos:
        repo_cfg = cfg.repo(repo_name)
        if repo_cfg is None:
            lines.append(f"- {repo_name}: (not found in coordinator.yml — check the mapping)")
            continue
        machines = [m.name for m in cfg.machines if m.can_work_on(repo_name)]
        depends = ", ".join(repo_cfg.depends_on) if repo_cfg.depends_on else "(none)"
        lines.append(
            f"- {repo_cfg.name} ({repo_cfg.github}): depends_on={depends}; "
            f"machines={', '.join(machines) if machines else '(none configured)'}"
        )
    return "\n".join(lines) if lines else "(no mapped repos)"


def build_decomposition_chat_briefing(
    *,
    submission: dict[str, Any],
    topology_context: str,
    discuss: bool,
    discuss_reason: str,
    running_context_section: str = "",
    house_stack_context_section: str = "",
) -> str:
    """Compose the seed briefing the worker sees as its first user message.

    Carries a MODE line (#2750, IL-4 — "say which mode was picked and why,
    in the first thing the operator sees") ahead of everything else, then
    exactly the four submission fields #2533's own body names (outcome /
    audience / done-definition / constraints) plus the mapped repo(s),
    `coordinator.yml` topology context, and the running-context ledger
    section — identical substance to what the Approved-work-items detail
    pane already shows the operator (ms-67 contract §4b: "nothing hidden
    between 'looks right in the panel' and 'is what the session got'"),
    plus everything #2749's ledger has accumulated across prior iterations.

    **#2997:** also carries a HOUSE STACK section (*house_stack_context_section*,
    normally :func:`house_stack_context`'s output) — the fleet's existing
    stack and infrastructure conventions, which used to be entirely absent
    from this briefing. That gap is what let a MODE: DISCUSS iteration on a
    greenfield repo propose a brand-new vendor (Supabase) without ever
    weighing the Cloudflare stack the rest of this org already runs and
    pays for (SUB-1EA1D3, this issue's own measured regression case). Empty
    by default so every existing caller/test keeps working unchanged.
    """
    submission_id = submission.get("submission_id", "")
    repos = submission.get("repos") or []
    mode_word = "DISCUSS" if discuss else "FILE"
    parts: list[str] = []
    parts.append(f"MODE: {mode_word} — {discuss_reason}")
    parts.append("")
    parts.append(f"=== Decomposition chat context for submission {submission_id} ===\n")
    parts.append(f"Client: {submission.get('client', '')}")
    parts.append(
        f"Project: {submission.get('project_id', '')} ({submission.get('project_label', '')})"
    )
    parts.append("")
    parts.append("OUTCOME:")
    parts.append(submission.get("outcome", "") or "(none provided)")
    parts.append("")
    parts.append("AUDIENCE:")
    parts.append(submission.get("audience", "") or "(none provided)")
    parts.append("")
    parts.append("DONE DEFINITION:")
    parts.append(submission.get("done_definition", "") or "(none provided)")
    parts.append("")
    parts.append("CONSTRAINTS:")
    parts.append(submission.get("constraints", "") or "(none provided)")
    parts.append("")
    parts.append(f"MAPPED REPO(S): {', '.join(repos) if repos else '(none)'}")
    parts.append("")
    parts.append("COORDINATOR.YML TOPOLOGY CONTEXT:")
    parts.append(topology_context)
    parts.append("")
    parts.append(
        house_stack_context_section.strip()
        or "HOUSE STACK (fleet-wide, #2997): (not computed for this briefing)"
    )
    parts.append("")
    parts.append(
        running_context_section.strip()
        or "RUNNING CONTEXT (from the portal ledger): (none yet — first iteration)"
    )
    parts.append("")
    parts.append("---")
    if discuss:
        parts.append(
            "This is a MODE: DISCUSS iteration: end in exactly one of Ask / "
            "Propose / Decompose — see your system prompt for the exact "
            "commands and rules for each. Open your final response by "
            "restating the MODE line above and which of the three you chose."
        )
    else:
        parts.append(
            "Decide whether this is oracle-loop-shaped work (docs/ORACLE_LOOP.md) "
            "or small enough to skip straight to normal dispatch, then produce one "
            "or more GitHub issues via `coord issue create` and queue them via "
            "`coord drive-queue add`. Once queued, record the portal link via "
            "`coord portal link` — see your system prompt for the exact command "
            "and the one-off-issue caveat."
        )
    return "\n".join(parts)


def resolve_approved_submission(config: "Config", submission_id: str) -> dict[str, Any] | None:
    """*submission_id*'s row from :func:`coord.approved_work.
    approved_submissions`, or ``None`` if it isn't currently ``"approved"``.

    Routed through the daemon's ``GET /board`` (``approved_submissions`` is a
    sibling projection key computed server-side, #2532) when this machine is
    a thin client (#2751-style exception, needed for #2750's ``--interactive``
    flavour, which — unlike the headless CLI dispatch path — is allowed to
    run on any machine that claims the submission's repo(s), not just the
    daemon host). :func:`coord.approved_work.approved_submissions` itself
    reads local SQLite directly with no daemon-awareness of its own, so a
    thin-client caller that invoked it unguarded would silently read that
    machine's own empty ``~/.coord/coord.db`` — exactly the #2336 failure
    mode every other portal command in :mod:`coord.commands.portal` guards
    against with ``_refuse_if_thin_client``. This path refuses to be wrong
    instead: it fetches the real answer from the daemon.
    """
    from coord import board_service  # noqa: PLC0415

    svc = board_service.resolve()
    if svc is not None:
        from coord.client import fetch_board_payload  # noqa: PLC0415

        rows = fetch_board_payload(svc).get("approved_submissions") or []
    else:
        from coord.approved_work import approved_submissions  # noqa: PLC0415

        rows = approved_submissions(config)

    return next(
        (
            r
            for r in rows
            if r.get("submission_id") == submission_id
            and r.get("signoff_status") == "approved"
        ),
        None,
    )


def describe_unapproved_submission(config: "Config", submission_id: str) -> str:
    """Human-readable reason :func:`resolve_approved_submission` returned
    ``None`` for *submission_id* (#2996).

    Names the disqualifying ``last_status`` when that is actually why — most
    often an operator's own ``coord portal enqueue-status`` push to a
    :data:`coord.approved_work._PULLED_STATUSES` value on a submission that
    had not, in fact, been decomposed yet — instead of the generic "is not a
    currently-approved portal submission", which names neither the cause nor
    the status that caused it (the whole complaint in #2996's issue body).

    Best-effort: the disqualifying-status lookup
    (:func:`coord.approved_work.disqualifying_status`) needs to read the
    daemon's own local :mod:`coord.portal_store` directly, so it is skipped
    (falling back to the plain generic message) when this machine is a thin
    client — the same constraint :func:`resolve_approved_submission` itself
    already works around by routing through the daemon's ``/board`` instead
    of reading the wrong box's empty tables.

    **This function must never raise.** It exists only to phrase a failure
    its callers have already decided on — ``dispatch_decomposition_chat``
    raises ``RuntimeError(describe_unapproved_submission(...))`` and
    ``_run_decompose_chat_interactive`` prints it — so an exception escaping
    the *enrichment* lookup would replace a clear, actionable error with a
    traceback about a completely different subsystem. The store read is
    therefore best-effort: any failure (an unreadable/locked portal store, a
    schema older than :func:`coord.approved_work.disqualifying_status`
    expects, a thin client that slipped past the ``board_service`` check)
    degrades to the plain generic message, which is exactly the pre-#2996
    behaviour and still correct — just less specific.
    """
    base = f"submission {submission_id!r} is not a currently-approved portal submission"

    reason: str | None = None
    try:
        from coord import board_service  # noqa: PLC0415

        if board_service.resolve() is None:
            from coord.approved_work import disqualifying_status  # noqa: PLC0415

            reason = disqualifying_status(submission_id)
    except Exception:  # noqa: BLE001 — best-effort enrichment, never break the message
        _log.debug(
            "describe_unapproved_submission: disqualifying-status lookup failed "
            "for %s; falling back to the generic message",
            submission_id,
            exc_info=True,
        )
        reason = None

    if reason:
        return (
            f"{base} — its last_status is {reason!r}, which coord already treats "
            "as pulled into decomposition/delivery (coord.approved_work."
            "_PULLED_STATUSES), so it no longer shows up in the TUI's Approved "
            "work items panel and this command refuses it. If it was pushed to "
            f"that status before decomposition actually happened, `coord portal "
            f"enqueue-status {submission_id} in-design` puts it back on the "
            "queue without re-mailing the customer. Nothing to decompose."
        )
    return (
        f"{base} (coord.approved_work.approved_submissions) — nothing to decompose"
    )


def dispatch_decomposition_chat(
    submission_id: str,
    config: "Config",
    *,
    machine_override: str | None = None,
    discuss: bool | None = None,
) -> tuple[str, str]:
    """End-to-end: look up *submission_id*, pick a machine, seed the
    briefing, dispatch a ``type="decomposition-chat"`` assignment. Returns
    ``(assignment_id, machine_name)``.

    *discuss* is ``--discuss``/``--no-discuss``/unset from the CLI — see
    :func:`select_discuss_mode` for how it combines with the mechanical
    under-specified/greenfield triggers to pick MODE: DISCUSS vs MODE: FILE.

    Raises ``RuntimeError`` when the submission isn't a currently-approved
    one, has no mapped repo, no machine claims every mapped repo (or the
    forced ``machine_override`` doesn't), or the agent rejects the dispatch.

    **#2661 note:** :func:`coord.approved_work.approved_submissions` now also
    returns never-signed-off ``signoff_status == "new"`` rows — that widening
    is correct for the TUI panel (a FIFO backlog of "not started yet" should
    show requests nobody has touched), but it is wrong for *this* function.
    Filing real GitHub issues and queuing real dispatch work is exactly the
    "customer signed off" gate this module exists to enforce, so only
    ``signoff_status == "approved"`` rows are eligible here — a ``"new"`` row
    is treated identically to a submission this function has never heard of.
    """
    submission = resolve_approved_submission(config, submission_id)
    if submission is None:
        raise RuntimeError(describe_unapproved_submission(config, submission_id))

    repos: list[str] = submission.get("repos") or []
    if not repos:
        raise RuntimeError(
            f"submission {submission_id!r} has no mapped repo (portal.project_repos "
            "in coordinator.yml) — map its project before pulling it into a "
            "decomposition session"
        )

    if machine_override:
        machine = next((m for m in config.machines if m.name == machine_override), None)
        if machine is None:
            raise RuntimeError(f"machine {machine_override!r} not in coordinator.yml")
        missing = [r for r in repos if not machine.can_work_on(r)]
        if missing:
            raise RuntimeError(
                f"machine {machine_override!r} does not list repo(s) "
                f"{', '.join(missing)} — submission {submission_id!r} maps to "
                f"{', '.join(repos)}, all of which the target machine must claim"
            )
    else:
        picked = pick_decomposition_chat_machine(config, repos)
        if picked is None:
            raise RuntimeError(
                f"no single machine claims every repo submission {submission_id!r} "
                f"maps to ({', '.join(repos)}) — decomposition-chat refuses rather "
                "than dispatching a session that can't reach all of them "
                "(ms-67 contract §4c/§6.7)"
            )
        machine = picked

    topology_context = repo_topology_context(config, repos)
    house_stack = house_stack_context(config, exclude_repos=repos)
    discuss_mode, discuss_reason = select_discuss_mode(
        config, submission, discuss_override=discuss
    )
    running_context = render_running_context_section(fetch_running_context(submission_id))
    briefing = build_decomposition_chat_briefing(
        submission=submission,
        topology_context=topology_context,
        discuss=discuss_mode,
        discuss_reason=discuss_reason,
        running_context_section=running_context,
        house_stack_context_section=house_stack,
    )

    resolved_model = config.models.default
    from coord.models import Assignment, Proposal

    proposal = Proposal(
        id=0,
        machine_name=machine.name,
        repo_name=repos[0],
        # No existing issue yet — same 0 sentinel `new-issue-chat` uses.
        issue_number=0,
        issue_title=_issue_title(submission_id),
        rationale="decomposition-chat",
        briefing=briefing,
        model=resolved_model,
        type="decomposition-chat",
        required_gates=[],
    )

    from coord.dispatch import dispatch_with_retry
    from coord.state import record_dispatched_assignment

    response = dispatch_with_retry(
        proposal,
        config,
        max_retries=config.concurrency.max_retries,
        backoff_base=config.concurrency.backoff_base,
    )

    assignment_id = response.get("id") or uuid.uuid4().hex[:12]

    repo_cfg = config.repo(repos[0])
    asg = Assignment(
        machine_name=machine.name,
        repo_name=repos[0],
        issue_number=0,
        issue_title=_issue_title(submission_id),
        files_allowed=[],
        files_forbidden=[],
        briefing=briefing,
        assignment_id=assignment_id,
        status="running",
        dispatched_at=time.time(),
        type="decomposition-chat",
        model=resolved_model,
    )
    record_dispatched_assignment(
        assignment=asg,
        repo_github=repo_cfg.github if repo_cfg is not None else repos[0],
    )

    return assignment_id, machine.name
